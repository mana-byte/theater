"""Live source for the detached OpenCode server runtime."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

from theater.harness.contracts.channels import ChannelHealth, ChannelHealthState
from theater.harness.contracts.runtime import (
    ConnectionHealth,
    RuntimeExecutionState,
)
from theater.harness.contracts.source import Batch, Source
from theater.models import Status

_CHANNEL_ID = "opencode-server-live"
_MAX_LINEAGE = 16
_MAX_IDLE_MARKS = 16

#: Probe-pinned event facts for 1.18.29+c470c79: data-only SSE with the
#: session.status busy/idle pair, the terminal session.idle marker, and
#: message.updated carrying info.id (user) and info.parentID (assistant).
_STATUS_TYPES = {"busy": RuntimeExecutionState.ACTIVE, "idle": RuntimeExecutionState.IDLE}


class OpenCodeServerLiveSource(Source):
    """SSE-derived execution state; the transcript source stays completion authority."""

    def __init__(self) -> None:
        self._session_id: str | None = None
        self._execution_state = RuntimeExecutionState.UNKNOWN
        self._health = ConnectionHealth.UNOPENED
        self._active_message_id: str | None = None
        self._idle_observations = 0
        self._idle_marks: dict[str, int] = {}
        self._lineage: dict[str, str] = {}
        self._order: deque[str] = deque()
        self._confirmations: dict[str, asyncio.Future[None]] = {}
        self._revision = 0
        self._read_revision = -1

    @property
    def connection_health(self) -> ConnectionHealth:
        return self._health

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def current_execution_state(self) -> RuntimeExecutionState:
        return self._execution_state

    def active_message_id(self) -> str | None:
        return self._active_message_id

    def idle_observations(self) -> int:
        return self._idle_observations

    def pending_confirmations(self) -> int:
        return len(self._confirmations)

    def assistant_lineage(self, message_id: str) -> str | None:
        return self._lineage.get(message_id)

    def adopt(self, session_id: str, state: RuntimeExecutionState) -> None:
        """Bind the exact session; per-session state starts clean."""
        self._session_id = session_id
        self._execution_state = state
        self._active_message_id = None
        self._lineage.clear()
        self._order.clear()
        self.cancel_pending_confirmations()
        self._revision += 1

    def cancel_pending_confirmations(self) -> None:
        """Retire every in-flight admission; its waiter falls back to readback."""
        for future in self._confirmations.values():
            if not future.done():
                future.cancel()
        self._confirmations.clear()
        self._idle_marks.clear()

    def connected(self) -> None:
        self._health = ConnectionHealth.CONNECTED
        self._revision += 1

    def stream_lost(self) -> None:
        """The event stream died: idle is no longer provable from events."""
        self._health = ConnectionHealth.DEGRADED
        self._execution_state = RuntimeExecutionState.UNKNOWN
        self._revision += 1

    def disconnected(self) -> None:
        self._health = ConnectionHealth.DISCONNECTED
        self._execution_state = RuntimeExecutionState.UNKNOWN
        self._revision += 1

    def reconcile_succeeded(self) -> None:
        """Readback proved the session exact again; idle proof needs new events."""
        self._health = ConnectionHealth.CONNECTED
        self._revision += 1

    def note_submitted(self, message_id: str) -> None:
        """The 204 owns the session unless this exact turn already completed."""
        mark = self._idle_marks.pop(message_id, None)
        if mark is not None and self._idle_observations > mark:
            return
        self._execution_state = RuntimeExecutionState.ACTIVE
        self._active_message_id = message_id
        self._revision += 1

    def observe_status(self, state: RuntimeExecutionState | None, *, idle_baseline: int) -> None:
        """Exact HTTP status: idle proves; busy never overrules a newer idle."""
        if state is RuntimeExecutionState.IDLE:
            self._note_idle()
        elif state is RuntimeExecutionState.ACTIVE and self._idle_observations == idle_baseline:
            self._execution_state = RuntimeExecutionState.ACTIVE
            self._revision += 1

    def register_confirmation(self, message_id: str) -> asyncio.Future[None]:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._confirmations[message_id] = future
        return future

    def resolve_confirmation(self, message_id: str) -> None:
        """Mark the exact user message present in API state or SSE."""
        future = self._confirmations.pop(message_id, None)
        if future is not None and not future.done():
            future.set_result(None)

    def discard_confirmation(self, message_id: str) -> None:
        future = self._confirmations.pop(message_id, None)
        if future is not None and not future.done():
            future.cancel()
        self._idle_marks.pop(message_id, None)

    def feed(self, event: object) -> None:
        """Fold one probe-shaped SSE event into observable state."""
        properties = _properties(event)
        if properties is None or properties.get("sessionID") != self._session_id:
            return
        event_type = _event_type(event)
        if event_type == "session.status":
            status = _status_type(properties)
            state = _STATUS_TYPES.get(status) if status is not None else None
            if state is RuntimeExecutionState.IDLE:
                self._note_idle()
            elif state is not None:
                self._execution_state = state
                self._revision += 1
        elif event_type == "session.idle":
            self._note_idle()
        elif event_type == "message.updated":
            info = properties.get("info")
            if not isinstance(info, dict):
                return
            message_id = info.get("id")
            if isinstance(message_id, str):
                self._record_message(message_id, info)

    def _note_idle(self) -> None:
        self._execution_state = RuntimeExecutionState.IDLE
        self._active_message_id = None
        self._idle_observations += 1
        self._revision += 1

    def _record_message(self, message_id: str, info: dict[str, object]) -> None:
        if info.get("role") == "user":
            if message_id in self._confirmations:
                self.resolve_confirmation(message_id)
                if len(self._idle_marks) >= _MAX_IDLE_MARKS:
                    self._idle_marks.pop(next(iter(self._idle_marks)))
                self._idle_marks[message_id] = self._idle_observations
                self._revision += 1
            return
        if info.get("role") != "assistant":
            return
        parent = info.get("parentID")
        if not isinstance(parent, str) or parent == message_id:
            return
        if parent not in self._lineage and len(self._lineage) >= _MAX_LINEAGE:
            oldest = self._order.popleft()
            self._lineage.pop(oldest, None)
        if parent not in self._lineage:
            self._order.append(parent)
        self._lineage[parent] = message_id
        self._revision += 1

    async def read(self) -> Batch:
        progressed = self._revision != self._read_revision
        self._read_revision = self._revision
        return Batch(
            progressed=progressed,
            status=self._hint_status(),
        )

    def health_snapshot(self) -> tuple[ChannelHealth, ...]:
        state = (
            ChannelHealthState.HEALTHY
            if self._health is ConnectionHealth.CONNECTED
            else ChannelHealthState.DEGRADED
            if self._health is ConnectionHealth.UNOPENED
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

    def _hint_status(self) -> Status | None:
        if self._execution_state is RuntimeExecutionState.ACTIVE:
            return Status.WORKING
        if self._execution_state is RuntimeExecutionState.IDLE:
            return Status.IDLE
        return None


def _event_type(event: object) -> str | None:
    if isinstance(event, dict):
        value = event.get("type")
        return value if isinstance(value, str) else None
    return None


def _properties(event: object) -> dict[str, Any] | None:
    if not isinstance(event, dict):
        return None
    properties = event.get("properties")
    return properties if isinstance(properties, dict) else None


def _status_type(properties: dict[str, Any]) -> str | None:
    status = properties.get("status")
    if isinstance(status, dict):
        value = status.get("type")
        return value if isinstance(value, str) else None
    return None


__all__ = ["OpenCodeServerLiveSource"]
