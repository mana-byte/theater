"""Passive lifecycle observations from the stock OpenCode TUI."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from time import monotonic

from theater.harness.contracts.channels import ChannelHealth, ChannelHealthState
from theater.harness.contracts.runtime import ConnectionHealth, RuntimeNotification
from theater.harness.contracts.source import Batch, Source
from theater.models import Status

_CHANNEL_ID = "opencode-tui-live"
# The extension refreshes every second. A silent connection cannot retain idle forever.
_STATUS_MAX_AGE_SECONDS = 3.0


class OpenCodeTuiLiveSource(Source):
    """One bounded current status; durable OpenCode state owns history."""

    def __init__(self, trusted_session_id_provider: Callable[[], str | None]) -> None:
        self._trusted_session_id_provider = trusted_session_id_provider
        self._visible_scope: tuple[str | None, int] | None = None
        self._status: Status | None = None
        self._status_at = 0.0
        self._revision = 0
        self._read_revision = 0
        self._read_scope: tuple[str | None, int] | None = None
        self._connected = True
        self._accepted = 0
        self._activity: Callable[[], None] | None = None

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
        status = _status_for(notification)
        changed = scope != previous or status != self._status
        self._visible_scope = scope
        self._status = status
        self._status_at = monotonic()
        if changed:
            self._revision += 1
            self._accepted += 1
            self._notify()

    def disconnected(self) -> None:
        self._connected = False
        self._notify()

    async def read(self) -> Batch:
        self._read_scope = self._visible_scope
        status = self._current_status()
        progressed = status is not None and self._revision != self._read_revision
        self._read_revision = self._revision
        return self.validate_enrichment_batch(Batch(progressed=progressed, status=status))

    def validate_enrichment_batch(self, batch: Batch) -> Batch:
        # A sibling source can yield while the visible route, trusted identity,
        # connection or current status changes. Revalidate immediately before use.
        status = self._current_status()
        if self._read_scope != self._visible_scope or status is None:
            return Batch()
        return replace(batch, status=status)

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

    def _current_status(self) -> Status | None:
        scope = self._visible_scope
        if (
            not self._connected
            or scope is None
            or scope[0] is None
            or scope[0] != self._trusted_session_id()
            or monotonic() - self._status_at > _STATUS_MAX_AGE_SECONDS
        ):
            return None
        return self._status

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
            or event.get("type") != "session.status"
            or _event_session_id(event) != session_id
        ):
            return None
    return session_id, epoch


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


def _event_session_id(event: Mapping[str, object]) -> str | None:
    properties = event.get("properties")
    return _identifier(properties.get("sessionID")) if isinstance(properties, Mapping) else None


def _identifier(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() and len(value) <= 512 else None


__all__ = ["OpenCodeTuiLiveSource"]
