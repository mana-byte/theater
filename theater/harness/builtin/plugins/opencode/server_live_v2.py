"""Live source for the 2.x detached server: execution state read from `session.active`.

2.x's event stream is volatile by contract (protocol/src/groups/event.ts: overflow and
disconnection drop events), while `session.active` is the server's own account of what it runs.
"""

from __future__ import annotations

import time
from collections.abc import Mapping

from theater.harness.contracts.channels import ChannelHealth, ChannelHealthState
from theater.harness.contracts.runtime import ConnectionHealth, RuntimeExecutionState
from theater.harness.contracts.source import Batch, Source
from theater.models import Status

_CHANNEL_ID = "opencode-server-live"
#: A just-admitted prompt may not run yet; idle is unproven until it has been seen or the server
#: has stamped idle after it, or this lapses.
_ADMISSION_GRACE_SECONDS = 5.0
#: Consecutive failed reads after which the server counts as gone rather than hiccuping.
_DISCONNECTED_AFTER = 3


class OpenCodeV2LiveSource(Source):
    """Polled execution state; the transcript source stays completion authority."""

    def __init__(self) -> None:
        self._session_id: str | None = None
        self._state = RuntimeExecutionState.UNKNOWN
        self._health = ConnectionHealth.UNOPENED
        self._admitted_at: float | None = None
        #: The server's own stamp on that prompt, to compare with the server's idle stamp.
        self._admitted_created: int | None = None
        self._failures = 0
        self._revision = 0
        self._read_revision = -1

    @property
    def connection_health(self) -> ConnectionHealth:
        return self._health

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def current_execution_state(self) -> RuntimeExecutionState:
        return self._state

    @property
    def admission_unproven(self) -> bool:
        """Whether an admitted prompt has been neither seen running nor proven finished."""
        return self._admitted_at is not None

    def adopt(self, session_id: str, state: RuntimeExecutionState) -> None:
        self._session_id = session_id
        self._admitted_at = None
        self._set(state, self._health)

    def note_submitted(self, created: int | None = None) -> None:
        """``created`` is the server's stamp on the admitted prompt, when it reported one."""
        self._admitted_at = time.monotonic()
        self._admitted_created = created
        self._set(RuntimeExecutionState.ACTIVE, self._health)

    def observe(self, active: Mapping[str, object] | None, idle_at: int | None = None) -> None:
        """One `session.active` read, with the session's idle stamp when it was read too.

        None is a failed read.
        """
        if active is None:
            self._failures += 1
            health = (
                ConnectionHealth.DISCONNECTED
                if self._failures >= _DISCONNECTED_AFTER
                else ConnectionHealth.DEGRADED
            )
            self._set(RuntimeExecutionState.UNKNOWN, health)
            return
        self._failures = 0
        if self._session_id in active:
            self._admitted_at = None
            self._set(RuntimeExecutionState.ACTIVE, ConnectionHealth.CONNECTED)
            return
        if self._admission_pending(idle_at):
            self._set(RuntimeExecutionState.ACTIVE, ConnectionHealth.CONNECTED)
            return
        self._admitted_at = None
        self._set(RuntimeExecutionState.IDLE, ConnectionHealth.CONNECTED)

    def _admission_pending(self, idle_at: int | None) -> bool:
        """A prompt too quick for any poll to see ends with the server's idle stamp after it.

        The server stamps idle only once its loop has drained, so that stamp also covers any
        work queued behind the prompt; without it the grace is all that bounds the wait.
        """
        admitted = self._admitted_at
        if admitted is None:
            return False
        created = self._admitted_created
        if idle_at is not None and created is not None and idle_at > created:
            return False
        return time.monotonic() - admitted < _ADMISSION_GRACE_SECONDS

    def disconnected(self) -> None:
        self._set(RuntimeExecutionState.UNKNOWN, ConnectionHealth.DISCONNECTED)

    async def read(self) -> Batch:
        progressed = self._revision != self._read_revision
        self._read_revision = self._revision
        return Batch(progressed=progressed, status=self._hint_status())

    def health_snapshot(self) -> tuple[ChannelHealth, ...]:
        state = (
            ChannelHealthState.HEALTHY
            if self._health is ConnectionHealth.CONNECTED
            else ChannelHealthState.DEGRADED
            if self._health in {ConnectionHealth.UNOPENED, ConnectionHealth.DEGRADED}
            else ChannelHealthState.FAILED
        )
        diagnostics = () if self._health is ConnectionHealth.CONNECTED else (self._health.value,)
        return (
            ChannelHealth(
                channel_id=_CHANNEL_ID,
                state=state,
                diagnostics=diagnostics,
                accepted=self._session_id is not None,
            ),
        )

    def _set(self, state: RuntimeExecutionState, health: ConnectionHealth) -> None:
        if (state, health) != (self._state, self._health):
            self._state, self._health = state, health
            self._revision += 1

    def _hint_status(self) -> Status | None:
        if self._state is RuntimeExecutionState.ACTIVE:
            return Status.WORKING
        if self._state is RuntimeExecutionState.IDLE:
            return Status.IDLE
        return None


__all__ = ["OpenCodeV2LiveSource"]
