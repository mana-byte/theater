"""Bounded protocol values for the Pi frontend runtime."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from theater.harness.contracts.runtime import (
    NativeHumanInteraction,
    NativeInteractionKind,
    RuntimeExecutionState,
    RuntimeSettingField,
    RuntimeSettings,
)

from .frontend import PI_FRONTEND_MAX_VALUE_CHARS, PI_FRONTEND_PROTOCOL
from .runtime_constants import PI_FRONTEND_HISTORY_MAX


class PiFrontendProtocolError(ValueError):
    """A bridge frame did not meet the bounded frontend protocol."""


@runtime_checkable
class PiFrontendPeer(Protocol):
    """Injected authenticated connection to one Pi extension bridge.

    The daemon host owns framing, auth, and buffering; Pi sees only request/notify/close.
    """

    async def request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        timeout: float,
    ) -> Mapping[str, object]:
        """Send one correlated request to the live extension."""
        ...

    def notifications(self) -> AsyncIterator[Mapping[str, object]]:
        """Yield extension ``event``, ``snapshot``, and ``history`` frames."""
        ...

    async def aclose(self) -> None:
        """Disconnect Theater only; never terminate Pi or its native work."""
        ...


@dataclass(frozen=True, slots=True)
class _FrontendSnapshot:
    native_session_id: str
    bridge_epoch: int
    snapshot_revision: int
    sequence: int
    settings: RuntimeSettings
    execution_state: RuntimeExecutionState
    native_turn_id: str | None
    settings_available: bool
    send_available: bool
    interrupt_available: bool
    model_update_available: bool
    reasoning_effort_update_available: bool
    pending_interaction: NativeHumanInteraction | None = None


def _bounded_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > PI_FRONTEND_MAX_VALUE_CHARS:
        raise PiFrontendProtocolError(f"Pi frontend {label} must be a bounded non-blank string")
    return value


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _bounded_string(value, label)


def _decode_execution_state(value: object) -> RuntimeExecutionState:
    if not isinstance(value, str):
        raise PiFrontendProtocolError("Pi frontend execution_state is invalid")
    try:
        return RuntimeExecutionState(value)
    except ValueError as exc:
        raise PiFrontendProtocolError("Pi frontend execution_state is invalid") from exc


def _decode_pending_interaction(value: object) -> NativeHumanInteraction | None:
    """Decode the bridge-reported pending human interaction, if any.

    The extension reports a pending question tool while one is blocked on
    the human; a missing key decodes as no interaction for older bridges.
    """
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise PiFrontendProtocolError("Pi frontend pending_interaction must be an object or null")
    kind_value = value.get("kind")
    if not isinstance(kind_value, str):
        raise PiFrontendProtocolError("Pi frontend pending_interaction kind is invalid")
    try:
        kind = NativeInteractionKind(kind_value)
    except ValueError as exc:
        raise PiFrontendProtocolError("Pi frontend pending_interaction kind is invalid") from exc
    native_turn_id = value.get("native_turn_id")
    if native_turn_id is not None and (
        not isinstance(native_turn_id, str) or len(native_turn_id) > PI_FRONTEND_MAX_VALUE_CHARS
    ):
        raise PiFrontendProtocolError("Pi frontend pending_interaction native_turn_id is invalid")
    details = value.get("details")
    if details is None:
        details = ""
    if not isinstance(details, str) or len(details) > PI_FRONTEND_MAX_VALUE_CHARS:
        raise PiFrontendProtocolError("Pi frontend pending_interaction details is invalid")
    return NativeHumanInteraction(kind=kind, native_turn_id=native_turn_id, details=details)


def _decode_bridge_epoch(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise PiFrontendProtocolError(f"Pi frontend {label} is invalid")
    return value


def _decode_sequence(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise PiFrontendProtocolError(f"Pi frontend {label} is invalid")
    return value


def _decode_settings(value: object, *, reasoning_effort_update: bool) -> RuntimeSettings:
    if not isinstance(value, Mapping):
        raise PiFrontendProtocolError("Pi frontend snapshot has no settings object")
    return RuntimeSettings(
        model=_optional_string(value.get("model"), "settings model"),
        reasoning_effort=_optional_string(value.get("reasoning_effort"), "settings reasoning"),
        supported_fields=(
            frozenset({RuntimeSettingField.REASONING_EFFORT})
            if reasoning_effort_update
            else frozenset()
        ),
    )


def _decode_snapshot(value: object) -> _FrontendSnapshot:
    if not isinstance(value, Mapping):
        raise PiFrontendProtocolError("Pi frontend snapshot must be an object")
    if value.get("protocol") != PI_FRONTEND_PROTOCOL:
        raise PiFrontendProtocolError("Pi frontend snapshot has an unsupported protocol")
    capabilities = value.get("capabilities")
    interrupt_capability = (
        capabilities.get("interrupt", False) if isinstance(capabilities, Mapping) else False
    )
    if (
        not isinstance(capabilities, Mapping)
        or not isinstance(capabilities.get("settings_update"), bool)
        or not isinstance(capabilities.get("model_update"), bool)
        or not isinstance(capabilities.get("reasoning_effort_update"), bool)
        or not isinstance(capabilities.get("send"), bool)
        or not isinstance(interrupt_capability, bool)
    ):
        raise PiFrontendProtocolError("Pi frontend snapshot has invalid capabilities")
    turn_id = value.get("native_turn_id")
    if turn_id is not None:
        turn_id = _bounded_string(turn_id, "snapshot native turn id")
    return _FrontendSnapshot(
        native_session_id=_bounded_string(value.get("native_session_id"), "native session id"),
        bridge_epoch=_decode_bridge_epoch(value.get("bridge_epoch"), "snapshot bridge epoch"),
        snapshot_revision=_decode_bridge_epoch(value.get("snapshot_revision"), "snapshot revision"),
        sequence=_decode_sequence(value.get("sequence"), "snapshot sequence"),
        settings=_decode_settings(
            value.get("settings"),
            reasoning_effort_update=capabilities["reasoning_effort_update"],
        ),
        execution_state=_decode_execution_state(value.get("execution_state")),
        native_turn_id=turn_id,
        settings_available=capabilities["settings_update"],
        send_available=capabilities["send"],
        interrupt_available=interrupt_capability,
        model_update_available=capabilities["model_update"],
        reasoning_effort_update_available=capabilities["reasoning_effort_update"],
        pending_interaction=_decode_pending_interaction(value.get("pending_interaction")),
    )


def _decode_event(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PiFrontendProtocolError("Pi frontend event must be an object")
    _bounded_string(value.get("name"), "event name")
    _bounded_string(value.get("native_session_id"), "event native session id")
    _decode_bridge_epoch(value.get("bridge_epoch"), "event bridge epoch")
    _decode_sequence(value.get("sequence"), "event sequence")
    return value


def _decode_notification(value: object) -> tuple[str, Mapping[str, object]]:
    if not isinstance(value, Mapping):
        raise PiFrontendProtocolError("Pi frontend notification must be an object")
    kind = value.get("type")
    if kind == "snapshot":
        snapshot = value.get("snapshot")
        _decode_snapshot(snapshot)
        assert isinstance(snapshot, Mapping)
        return kind, snapshot
    if kind == "event":
        event = value.get("event")
        decoded = _decode_event(event)
        return kind, decoded
    if kind == "history":
        events = value.get("events")
        if not isinstance(events, (list, tuple)) or len(events) > PI_FRONTEND_HISTORY_MAX:
            raise PiFrontendProtocolError("Pi frontend history exceeds its event bound")
        for event in events:
            _decode_event(event)
        snapshot = value.get("snapshot")
        if snapshot is not None:
            _decode_snapshot(snapshot)
        return kind, value
    raise PiFrontendProtocolError("Pi frontend notification type is unsupported")
