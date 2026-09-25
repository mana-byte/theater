"""Pi frontend notification and snapshot reconciliation."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from theater.harness.contracts.runtime import (
    ConnectionHealth,
    RuntimeExecutionState,
    RuntimeSettings,
)

from ._runtime_host import PiFrontendRuntimeHost
from .runtime_protocol import (
    PiFrontendPeer,
    PiFrontendProtocolError,
    _bounded_string,
    _decode_bridge_epoch,
    _decode_event,
    _decode_execution_state,
    _decode_notification,
    _decode_snapshot,
    _FrontendSnapshot,
)


class PiFrontendRuntimeNotifications(PiFrontendRuntimeHost):
    _receive_task: asyncio.Task[None] | None
    _bridge_epoch: int | None
    _snapshot_revision: int | None
    _native_session_id: str | None

    def _start_receiver(self) -> None:
        if self._closed or self._peer is None:
            return
        if self._receive_task is not None and not self._receive_task.done():
            return
        peer = self._peer
        peer_generation = self._peer_generation
        self._receive_task = asyncio.create_task(
            self._receive_notifications(peer, peer_generation),
            name=f"pi-frontend-{self._participant_id}",
        )

    async def _receive_notifications(self, peer: PiFrontendPeer, peer_generation: int) -> None:
        try:
            async for frame in peer.notifications():
                if (
                    self._closed
                    or peer is not self._peer
                    or peer_generation != self._peer_generation
                ):
                    return
                try:
                    kind, payload = _decode_notification(frame)
                    if self._apply_notification(kind, payload):
                        self._accepted += 1
                    else:
                        self._dropped += 1
                except PiFrontendProtocolError as exc:
                    self._dropped += 1
                    self._diagnostic(str(exc))
                    self._health = ConnectionHealth.DEGRADED
                    self._touch()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._stream_identity_current(peer, peer_generation):
                self._mark_disconnected(
                    f"Pi frontend notification stream failed: {type(exc).__name__}: {exc}"
                )
        else:
            if not self._closed and self._stream_identity_current(peer, peer_generation):
                self._mark_disconnected("Pi frontend notification stream closed")

    def _apply_notification(self, kind: str, payload: Mapping[str, object]) -> bool:
        if kind == "snapshot":
            return self._apply_snapshot(_decode_snapshot(payload))
        if kind == "event":
            return self._apply_event(payload)
        snapshot = payload.get("snapshot")
        applied = False
        if snapshot is not None:
            applied = self._apply_snapshot(_decode_snapshot(snapshot))
        events = payload["events"]
        if not isinstance(events, list | tuple):
            raise PiFrontendProtocolError("Pi frontend history events must be a sequence")
        for event in events:
            applied = self._apply_event(_decode_event(event)) or applied
        return applied

    def _apply_snapshot(self, snapshot: _FrontendSnapshot) -> bool:
        """Apply an exact current snapshot without allowing identity rollback.

        A higher epoch may start the next session; older frames are noise and must not change state.
        """
        current_epoch = self._bridge_epoch
        if current_epoch is None:
            self._bridge_epoch = snapshot.bridge_epoch
            self._native_session_id = snapshot.native_session_id
            self._snapshot_revision = snapshot.snapshot_revision
            self._session_epoch += 1
            self._last_sequence = snapshot.sequence
        elif snapshot.bridge_epoch < current_epoch:
            return False
        elif snapshot.bridge_epoch == current_epoch:
            if (
                self._native_session_id != snapshot.native_session_id
                or self._snapshot_revision is None
                or snapshot.snapshot_revision < self._snapshot_revision
                or snapshot.sequence < self._last_sequence
            ):
                return False
            self._snapshot_revision = snapshot.snapshot_revision
            self._last_sequence = max(self._last_sequence, snapshot.sequence)
        else:
            self._bridge_epoch = snapshot.bridge_epoch
            self._native_session_id = snapshot.native_session_id
            self._snapshot_revision = snapshot.snapshot_revision
            # A newer extension generation invalidates an in-flight receipt
            # even when Pi happened to retain the same session identifier.
            self._session_epoch += 1
            self._last_sequence = snapshot.sequence
        self._settings = snapshot.settings
        self._execution_state = snapshot.execution_state
        self._pending_interaction = snapshot.pending_interaction
        self._native_turn_id = snapshot.native_turn_id
        self._settings_available = (
            snapshot.settings_available and snapshot.reasoning_effort_update_available
        )
        self._send_available = snapshot.send_available
        self._interrupt_available = snapshot.interrupt_available
        self._health = ConnectionHealth.CONNECTED
        self._touch()
        return True

    def _apply_event(self, event: Mapping[str, object]) -> bool:
        name = _bounded_string(event.get("name"), "event name")
        session_id = _bounded_string(event.get("native_session_id"), "event native session id")
        bridge_epoch = _decode_bridge_epoch(event.get("bridge_epoch"), "event bridge epoch")
        sequence = event.get("sequence")
        assert type(sequence) is int
        # Events are never identity authority.  Require the exact snapshot's
        # live session and epoch before even considering sequence/state.
        if (
            self._bridge_epoch is None
            or bridge_epoch != self._bridge_epoch
            or self._native_session_id is None
            or session_id != self._native_session_id
            or sequence <= self._last_sequence
        ):
            return False
        self._last_sequence = sequence
        return self._apply_state_event(name, event)

    def _apply_state_event(self, name: str, event: Mapping[str, object]) -> bool:
        if name == "session_shutdown":
            # Keep epoch/sequence through shutdown.  Only a newer exact
            # snapshot (or explicit peer reconnect) can establish a successor.
            self._native_session_id = None
            self._session_epoch += 1
            self._execution_state = RuntimeExecutionState.UNKNOWN
            self._pending_interaction = None
            self._settings = RuntimeSettings()
            self._settings_available = False
            self._send_available = False
            self._interrupt_available = False
            self._native_turn_id = None
            self._touch()
            return True
        if name in {"before_agent_start", "agent_start", "session_before_compact", "agent_end"}:
            # ``agent_end`` is intentionally still active: retries,
            # compaction/retry, and queued continuations have not reached the
            # public outer-settled boundary yet.  A turn boundary also closes
            # any question the previous turn was blocked on.
            self._execution_state = RuntimeExecutionState.ACTIVE
            self._pending_interaction = None
            self._touch()
            return True
        if name == "agent_settled":
            # ``agent_settled`` is Pi's outer lifecycle boundary, but retain
            # the extension's live ``ctx.isIdle()`` read: an unexpected false
            # read is unknown, never permission to manufacture idle.
            reported_state = event.get("execution_state")
            self._execution_state = (
                _decode_execution_state(reported_state)
                if reported_state is not None
                else RuntimeExecutionState.UNKNOWN
            )
            # Settled is the outer boundary: a question that was pending is
            # answered or abandoned by now, and the snapshot that follows
            # each event would re-report it if it were somehow still open.
            self._pending_interaction = None
            self._touch()
            return True
        reported_state = event.get("execution_state")
        if reported_state is not None:
            self._execution_state = _decode_execution_state(reported_state)
            self._touch()
        return True

    def _reset_for_peer_reconnect(self) -> None:
        """Forget peer-local ordering only after an explicit host replacement."""
        self._bridge_epoch = None
        self._snapshot_revision = None
        self._native_session_id = None
        self._native_turn_id = None
        self._session_epoch += 1
        self._last_sequence = -1
        self._settings = RuntimeSettings()
        self._execution_state = RuntimeExecutionState.UNKNOWN
        self._pending_interaction = None
        self._settings_available = False
        self._send_available = False
        self._interrupt_available = False
        self._health = ConnectionHealth.UNOPENED
        self._touch()
