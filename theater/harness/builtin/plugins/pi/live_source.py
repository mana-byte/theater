"""Live status source for the Pi frontend runtime."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from theater.harness.contracts.channels import ChannelHealth, ChannelHealthState
from theater.harness.contracts.runtime import ConnectionHealth, RuntimeExecutionState
from theater.harness.contracts.source import Batch, Source
from theater.models import Status

from ._runtime_host import PiFrontendRuntimeHost
from .runtime_constants import PI_FRONTEND_CHANNEL_ID


class PiFrontendLiveSource(Source):
    """Live frontend facts; the durable Pi transcript stays completion authority."""

    def __init__(self, runtime: PiFrontendRuntimeHost) -> None:
        self._runtime = runtime
        self._last_revision = -1
        self._last_status: Status | None = None
        self._read_scope: tuple | None = None

    def set_activity_callback(self, callback: Callable[[], None] | None) -> None:
        self._runtime.set_activity_callback(callback)

    async def read(self) -> Batch:
        status = self._status()
        revision = self._runtime._revision
        progressed = revision != self._last_revision or status != self._last_status
        self._last_revision = revision
        self._last_status = status
        self._read_scope = self._scope()
        return Batch(
            progressed=progressed,
            status=status,
        )

    def _scope(self) -> tuple:
        runtime = self._runtime
        return (runtime._native_session_id, runtime._session_epoch, runtime._peer_generation)

    def validate_enrichment_batch(self, batch: Batch) -> Batch:
        if not self._runtime._trusted_session_matches() or self._read_scope != self._scope():
            return Batch()
        return replace(batch, status=self._status())

    def health_snapshot(self) -> tuple[ChannelHealth, ...]:
        health = self._runtime._runtime_snapshot().health
        if health is ConnectionHealth.CONNECTED:
            state = ChannelHealthState.HEALTHY
        elif health is ConnectionHealth.DEGRADED:
            state = ChannelHealthState.DEGRADED
        elif health is ConnectionHealth.DISCONNECTED:
            state = ChannelHealthState.FAILED
        else:
            state = ChannelHealthState.STARTING
        return (
            ChannelHealth(
                channel_id=PI_FRONTEND_CHANNEL_ID,
                state=state,
                diagnostics=tuple(self._runtime._diagnostics),
                accepted=self._runtime._accepted,
                dropped=self._runtime._dropped,
            ),
        )

    def _status(self) -> Status | None:
        runtime = self._runtime
        if not runtime._trusted_session_matches():
            return None
        if runtime._health is not ConnectionHealth.CONNECTED:
            return None
        if runtime._pending_interaction is not None:
            # Display hint only; never a control decision input.  A question
            # tool the native UI owns is parked on the human, exactly like
            # Codex's pending clarification.
            return Status.AWAITING_INPUT
        if runtime._execution_state is RuntimeExecutionState.IDLE:
            return Status.IDLE
        if runtime._execution_state is RuntimeExecutionState.ACTIVE:
            return Status.WORKING
        return None
