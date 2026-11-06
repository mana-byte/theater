"""Native Claude state and evidence, acknowledged only after daemon persistence."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from typing import TYPE_CHECKING

from theater.harness.contracts.channels import ChannelHealth, ChannelHealthState
from theater.harness.contracts.runtime import (
    ConnectionHealth,
    NativeTurnOutcome,
    RuntimeExecutionState,
)
from theater.harness.contracts.source import Batch, Source
from theater.models import Status

from .runtime_protocol import CHANNEL_ID, MAX_PENDING_OUTCOMES

if TYPE_CHECKING:
    from .runtime import ClaudeRuntime


class ClaudeLiveSource(Source):
    def __init__(self, runtime: ClaudeRuntime) -> None:
        self.runtime = runtime
        self._pending: OrderedDict[str, NativeTurnOutcome] = OrderedDict()
        self._inflight: tuple[NativeTurnOutcome, ...] = ()
        self._last_revision = -1
        self._callback: Callable[[], None] | None = None

    def set_activity_callback(self, callback: Callable[[], None] | None) -> None:
        self._callback = callback

    def changed(self) -> None:
        if self._callback is not None:
            self._callback()

    def append(self, evidence: NativeTurnOutcome) -> None:
        existing = self._pending.get(evidence.native_turn_id)
        if existing is not None:
            if existing != evidence:
                raise ValueError("Claude bridge changed an already observed native outcome")
            return
        if len(self._pending) >= MAX_PENDING_OUTCOMES:
            raise ValueError("Claude native evidence buffer is full; not discarding exact outcomes")
        self._pending[evidence.native_turn_id] = evidence
        self.changed()

    async def read(self) -> Batch:
        snapshot = self.runtime.current_snapshot()
        revision = self.runtime.revision
        changed = revision != self._last_revision
        self._last_revision = revision
        status = None
        if changed and snapshot.health is ConnectionHealth.CONNECTED:
            if snapshot.execution_state is RuntimeExecutionState.IDLE:
                status = Status.IDLE
            elif snapshot.execution_state is RuntimeExecutionState.ACTIVE:
                status = Status.WORKING
        self._inflight = tuple(self._pending.values())
        return Batch(
            progressed=changed or bool(self._inflight),
            status=status,
            terminal_evidence=self._inflight,
        )

    def pending_terminal_evidence(self) -> bool:
        return bool(self._inflight)

    def terminal_evidence_snapshot(self) -> tuple[NativeTurnOutcome, ...]:
        return self._inflight

    def buffered_terminal_evidence(self) -> tuple[NativeTurnOutcome, ...]:
        return tuple(self._pending.values())

    def terminal_evidence_delivered(self) -> None:
        turns = tuple(item.native_turn_id for item in self._inflight)
        for turn in turns:
            self._pending.pop(turn, None)
        self._inflight = ()
        if turns:
            self.runtime.acknowledge(turns)

    def health_snapshot(self) -> tuple[ChannelHealth, ...]:
        snapshot = self.runtime.current_snapshot()
        return (
            ChannelHealth(
                channel_id=CHANNEL_ID,
                state=(
                    ChannelHealthState.HEALTHY
                    if snapshot.health is ConnectionHealth.CONNECTED
                    else ChannelHealthState.FAILED
                ),
                diagnostics=snapshot.health_diagnostics,
            ),
        )
