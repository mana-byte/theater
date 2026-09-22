"""Passive lifecycle observations and exact turn lineage from the stock TUI."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from time import monotonic

from theater.harness.contracts.channels import ChannelHealth, ChannelHealthState
from theater.harness.contracts.runtime import (
    ConnectionHealth,
    NativeTurnOutcome,
    NativeTurnTerminal,
    RuntimeExecutionState,
    RuntimeNotification,
)
from theater.harness.contracts.source import Batch, Source
from theater.models import Status

from .inputs import snapshot_awaiting

_CHANNEL_ID = "opencode-tui-live"
# The extension refreshes every second. A silent connection cannot retain idle forever.
_STATUS_MAX_AGE_SECONDS = 3.0
_MAX_TRACKED_TURNS = 8
_MAX_STAGED_OUTCOMES = 32
_MAX_ORPHAN_LINEAGE = 32
_ABORT_ERROR_NAME = "MessageAbortedError"


@dataclass
class _TurnRecord:
    """One Theater-submitted user message awaiting its assistant lineage."""

    session_id: str
    epoch: int
    message_id: str
    assistant_id: str | None = None
    terminal: NativeTurnTerminal | None = None


@dataclass
class _OrphanLineage:
    """Terminal lineage that raced ahead of its submitted-turn record."""

    session_id: str
    epoch: int
    assistant_id: str | None
    outcome: NativeTurnOutcome


class OpenCodeTuiLiveSource(Source):
    """One bounded current status plus exact submitted-turn terminal evidence."""

    def __init__(self, trusted_session_id_provider: Callable[[], str | None]) -> None:
        self._trusted_session_id_provider = trusted_session_id_provider
        self._visible_scope: tuple[str | None, int] | None = None
        self._status: Status | None = None
        self._awaiting = False
        self._status_at = 0.0
        self._revision = 0
        self._read_revision = 0
        self._read_scope: tuple[str | None, int] | None = None
        self._connected = True
        self._accepted = 0
        self._activity: Callable[[], None] | None = None
        self._turns: OrderedDict[str, _TurnRecord] = OrderedDict()
        self._orphans: OrderedDict[str, _OrphanLineage] = OrderedDict()
        self._staged: list[NativeTurnOutcome] = []

    @property
    def connection_health(self) -> ConnectionHealth:
        if not self._connected:
            return ConnectionHealth.DISCONNECTED
        return (
            ConnectionHealth.CONNECTED
            if self._current_status() is not None
            else ConnectionHealth.DEGRADED
        )

    def set_activity_callback(self, callback: Callable[[], None] | None) -> None:
        self._activity = callback

    def feed(self, notification: RuntimeNotification) -> None:
        scope = _visible_scope_for(notification)
        if not self._connected or scope is None:
            return
        previous = self._visible_scope
        if previous is not None and (
            scope[1] < previous[1] or (scope[1] == previous[1] and scope != previous)
        ):
            return
        if _is_status_bearing(notification):
            status = _status_for(notification)
            awaiting = self._awaiting if scope == previous else False
            if notification.method == "snapshot":
                awaiting = snapshot_awaiting(notification.params)
            changed = scope != previous or status != self._status or awaiting != self._awaiting
            self._visible_scope = scope
            self._status = status
            self._awaiting = awaiting
            self._status_at = monotonic()
            if changed:
                self._revision += 1
                self._accepted += 1
                self._notify()
            return
        if scope != previous:
            # Lineage never answers for a status; a scope move invalidates it
            # until the next status-bearing notification restores one.
            self._visible_scope = scope
            self._status = None
            self._awaiting = False
            self._status_at = monotonic()
            self._revision += 1
            self._accepted += 1
            self._notify()
        if scope[0] is None:
            return
        self._accept_lineage(notification, scope[0], scope[1])

    def disconnected(self) -> None:
        self._connected = False
        self._notify()

    def control_scope(self) -> tuple[str, int] | None:
        """The current mutation scope when the visible route is the trusted session."""
        scope = self._visible_scope
        if not self._connected or scope is None or scope[0] is None:
            return None
        if scope[0] != self._trusted_session_id():
            return None
        return scope[0], scope[1]

    def current_execution_state(self) -> RuntimeExecutionState:
        status = self._current_status()
        if status is Status.WORKING:
            return RuntimeExecutionState.ACTIVE
        if status is Status.IDLE:
            return RuntimeExecutionState.IDLE
        return RuntimeExecutionState.UNKNOWN

    def note_submitted_turn(self, session_id: str, epoch: int, message_id: str) -> bool:
        """Record one accepted Theater turn; False when the scope already moved."""
        if self.control_scope() != (session_id, epoch):
            return False
        record = _TurnRecord(session_id=session_id, epoch=epoch, message_id=message_id)
        self._turns[message_id] = record
        while len(self._turns) > _MAX_TRACKED_TURNS:
            self._turns.popitem(last=False)
        orphan = self._orphans.pop(message_id, None)
        # A terminal event can beat the reply here; the parked orphan
        # resolves against its exact parent and epoch on this note.
        if orphan is not None and orphan.session_id == session_id and orphan.epoch == epoch:
            record.assistant_id = orphan.assistant_id
            record.terminal = orphan.outcome.terminal
            self._stage(orphan.outcome)
        return True

    def active_turn_id(self) -> str | None:
        """The newest submitted turn in the current scope without terminal evidence."""
        scope = self.control_scope()
        if scope is None:
            return None
        for record in reversed(self._turns.values()):
            if record.session_id == scope[0] and record.epoch == scope[1] and not record.terminal:
                return record.message_id
        return None

    async def read(self) -> Batch:
        self._read_scope = self._visible_scope
        status = self._hint_status()
        progressed = status is not None and (
            self._revision != self._read_revision or bool(self._staged)
        )
        self._read_revision = self._revision
        return self.validate_enrichment_batch(
            Batch(progressed=progressed, status=status, terminal_evidence=tuple(self._staged))
        )

    def validate_enrichment_batch(self, batch: Batch) -> Batch:
        # A sibling source can yield while the visible route, trusted identity,
        # connection or current status changes. Revalidate immediately before use.
        status = self._hint_status()
        if self._read_scope != self._visible_scope or status is None:
            return Batch()
        return replace(batch, status=status)

    def terminal_evidence_snapshot(self) -> tuple[NativeTurnOutcome, ...]:
        return tuple(self._staged)

    def terminal_evidence_delivered(self) -> None:
        self._staged.clear()

    def health_snapshot(self) -> tuple[ChannelHealth, ...]:
        health = self.connection_health
        state = (
            ChannelHealthState.HEALTHY
            if health is ConnectionHealth.CONNECTED
            else ChannelHealthState.DEGRADED
            if health is ConnectionHealth.DEGRADED
            else ChannelHealthState.FAILED
        )
        diagnostics = () if health is ConnectionHealth.CONNECTED else (health.value,)
        return (
            ChannelHealth(
                channel_id=_CHANNEL_ID,
                state=state,
                diagnostics=diagnostics,
                accepted=self._accepted,
            ),
        )

    def _accept_lineage(
        self, notification: RuntimeNotification, session_id: str, epoch: int
    ) -> None:
        event = notification.params.get("event")
        if not isinstance(event, Mapping) or event.get("type") != "message.updated":
            return
        properties = event.get("properties")
        if not isinstance(properties, Mapping):
            return
        info = properties.get("info")
        if not isinstance(info, Mapping) or info.get("role") != "assistant":
            return
        message_id = _identifier(properties.get("sessionID"))
        assistant_id = _identifier(info.get("id"))
        parent_id = _identifier(info.get("parentID"))
        if message_id != session_id or assistant_id is None or parent_id is None:
            return
        record = self._turns.get(parent_id)
        terminal = _terminal_for(info)
        if record is not None and record.session_id == session_id and record.epoch == epoch:
            record.assistant_id = assistant_id
            if terminal is None or record.terminal is not None:
                return
            record.terminal = terminal
            self._stage(_outcome_for(record, terminal, info))
            return
        if record is not None or terminal is None:
            return
        # The assistant finished before the turn was recorded; park it for the note.
        self._orphans[parent_id] = _OrphanLineage(
            session_id=session_id,
            epoch=epoch,
            assistant_id=assistant_id,
            outcome=_outcome_for(
                _TurnRecord(session_id=session_id, epoch=epoch, message_id=parent_id),
                terminal,
                info,
            ),
        )
        while len(self._orphans) > _MAX_ORPHAN_LINEAGE:
            self._orphans.popitem(last=False)

    def _stage(self, outcome: NativeTurnOutcome) -> None:
        if any(
            existing.native_turn_id == outcome.native_turn_id
            and existing.native_session_id == outcome.native_session_id
            for existing in self._staged
        ):
            return
        self._staged.append(outcome)
        while len(self._staged) > _MAX_STAGED_OUTCOMES:
            self._staged.pop(0)
        self._accepted += 1
        self._notify()

    def _hint_status(self) -> Status | None:
        if not self._status_scope_is_current():
            return None
        return Status.AWAITING_INPUT if self._awaiting else self._status

    def _current_status(self) -> Status | None:
        return self._status if self._status_scope_is_current() else None

    def _status_scope_is_current(self) -> bool:
        scope = self._visible_scope
        return (
            self._connected
            and scope is not None
            and scope[0] is not None
            and scope[0] == self._trusted_session_id()
            and monotonic() - self._status_at <= _STATUS_MAX_AGE_SECONDS
        )

    def _trusted_session_id(self) -> str | None:
        try:
            return _identifier(self._trusted_session_id_provider())
        except Exception:
            return None

    def _notify(self) -> None:
        if self._activity is not None:
            self._activity()


def _visible_scope_for(notification: RuntimeNotification) -> tuple[str | None, int] | None:
    if notification.method not in {"event", "snapshot"}:
        return None
    params = notification.params
    if "session_id" not in params or "route_session_id" not in params:
        return None
    session_id = _identifier(params.get("session_id"))
    route_session_id = _identifier(params.get("route_session_id"))
    epoch = params.get("session_epoch")
    if route_session_id != session_id or type(epoch) is not int or epoch < 0:
        return None
    if session_id is None:
        # Explicit home/non-session route clears previously observed status.
        if (
            notification.method == "snapshot"
            and params["session_id"] is None
            and params["route_session_id"] is None
        ):
            return None, epoch
        return None
    if epoch < 1:
        return None
    if notification.method == "event":
        event = params.get("event")
        if (
            not isinstance(event, Mapping)
            or event.get("type") not in {"session.status", "message.updated"}
            or _event_session_id(event) != session_id
        ):
            return None
    return session_id, epoch


def _is_status_bearing(notification: RuntimeNotification) -> bool:
    if notification.method == "snapshot":
        return True
    event = notification.params.get("event")
    return isinstance(event, Mapping) and event.get("type") == "session.status"


def _status_for(notification: RuntimeNotification) -> Status | None:
    if notification.method == "snapshot":
        return _status_value(notification.params.get("status"))
    event = notification.params.get("event")
    if not isinstance(event, Mapping):
        return None
    properties = event.get("properties")
    return _status_value(properties.get("status")) if isinstance(properties, Mapping) else None


def _status_value(value: object) -> Status | None:
    state = value.get("type") if isinstance(value, Mapping) else None
    if state in {"busy", "retry"}:
        return Status.WORKING
    if state == "idle":
        return Status.IDLE
    return None


def _terminal_for(info: Mapping[str, object]) -> NativeTurnTerminal | None:
    error = info.get("error")
    if isinstance(error, Mapping):
        name = error.get("name")
        return (
            NativeTurnTerminal.INTERRUPTED
            if name == _ABORT_ERROR_NAME
            else NativeTurnTerminal.FAILED
        )
    time = info.get("time")
    completed = time.get("completed") if isinstance(time, Mapping) else None
    if type(completed) is int and completed > 0:
        return NativeTurnTerminal.COMPLETED
    return None


def _outcome_for(
    record: _TurnRecord, terminal: NativeTurnTerminal, info: Mapping[str, object]
) -> NativeTurnOutcome:
    error_code: str | None = None
    error_text: str | None = None
    error = info.get("error")
    if isinstance(error, Mapping):
        error_code = _identifier(error.get("name"))
        error_text = _bounded_text(error.get("message"))
    return NativeTurnOutcome(
        native_session_id=record.session_id,
        native_turn_id=record.message_id,
        terminal=terminal,
        error_code=error_code,
        error=error_text,
        completed_at=_completed_at(info),
    )


def _completed_at(info: Mapping[str, object]) -> float | None:
    time = info.get("time")
    completed = time.get("completed") if isinstance(time, Mapping) else None
    return completed / 1000 if type(completed) is int and completed > 0 else None


def _event_session_id(event: Mapping[str, object]) -> str | None:
    properties = event.get("properties")
    return _identifier(properties.get("sessionID")) if isinstance(properties, Mapping) else None


def _identifier(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() and len(value) <= 512 else None


def _bounded_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:512]


__all__ = ["OpenCodeTuiLiveSource"]
