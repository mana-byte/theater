"""Public error vocabulary with forward-compatible decoding."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

type JSONScalar = bool | int | float | str | None
type JSONValue = JSONScalar | tuple[JSONValue, ...] | Mapping[str, JSONValue]


def _freeze(value: object) -> JSONValue:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("error values must not contain non-finite numbers")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("error object keys must be strings")
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    raise TypeError("error values must be JSON-compatible")


def _thaw(value: JSONValue) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


class ErrorCode(StrEnum):
    INTERNAL = "internal"
    UNKNOWN_METHOD = "unknown_method"
    BAD_REQUEST = "bad_request"
    NOT_FOUND = "not_found"
    TOO_LARGE = "too_large"
    BUSY = "busy"
    NOT_ADDRESSABLE = "not_addressable"
    HUMAN_PRESENT = "human_present"
    AWAITING_DECISION = "awaiting_decision"
    STALE_TARGET = "stale_target"
    TRANSCRIPT_UNTRUSTED = "transcript_untrusted"
    TRANSCRIPT_IDENTITY_LOST = "transcript_identity_lost"
    CAPABILITY_DENIED = "capability_denied"
    PLUGIN_AUTHENTICATION_FAILED = "plugin_auth_failed"
    NOT_YOUR_CHILD = "not_your_child"
    NO_SELF_KILL = "no_self_kill"
    NAME_TAKEN = "name_taken"
    DEPTH_EXCEEDED = "depth_exceeded"
    CYCLE_DETECTED = "cycle_detected"
    BUDGET_EXCEEDED = "budget_exceeded"
    MODEL_NOT_ALLOWED = "model_not_allowed"
    REASONING_NOT_ALLOWED = "reasoning_not_allowed"
    HANDSHAKE_REQUIRED = "handshake_required"
    INCOMPATIBLE_API = "incompatible_api"
    MISSING_CAPABILITY = "missing_capability"
    WRONG_CONNECTION_ROLE = "wrong_connection_role"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_BUSY = "provider_busy"
    STALE_GENERATION = "stale_generation"
    TERMINAL_IDENTITY_MISMATCH = "terminal_identity_mismatch"
    OWNERSHIP_CONFLICT = "ownership_conflict"
    WORKSPACE_IN_USE = "workspace_in_use"
    WORKSPACE_DELETING = "workspace_deleting"
    SNAPSHOT_EXPIRED = "snapshot_expired"
    RESNAPSHOT_REQUIRED = "resnapshot_required"


@dataclass(frozen=True, slots=True)
class ErrorValue:
    code: str
    message: str
    details: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def known_code(self) -> ErrorCode | None:
        try:
            return ErrorCode(self.code)
        except ValueError:
            return None

    def to_wire(self) -> dict[str, object]:
        result = {"code": self.code, "message": self.message, "details": _thaw(self.details)}
        result.update({key: _thaw(item) for key, item in self.extra.items() if key not in result})
        return result

    @classmethod
    def from_wire(cls, value: object) -> ErrorValue:
        if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
            raise TypeError("error must be an object with string keys")
        data = value
        code = data.get("code")
        message = data.get("message")
        if not isinstance(code, str) or not code:
            raise TypeError("error.code must be a non-empty string")
        if not isinstance(message, str):
            raise TypeError("error.message must be a string")
        details_value = _freeze(data.get("details", {}))
        if not isinstance(details_value, Mapping):
            raise TypeError("error.details must be an object")
        return cls(
            code=code,
            message=message,
            details=details_value,
            extra=MappingProxyType(
                {
                    str(key): _freeze(item)
                    for key, item in data.items()
                    if key not in {"code", "message", "details"}
                }
            ),
        )


class FrontendError(Exception):
    """An error returned by the public frontend API."""

    def __init__(self, value: ErrorValue):
        super().__init__(f"{value.code}: {value.message}")
        self.value = value


__all__ = ["ErrorCode", "ErrorValue", "FrontendError"]
