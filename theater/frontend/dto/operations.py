"""Durable public operation values, separate from agent jobs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from theater.frontend.dto._wire import (
    JSONValue,
    append_extras,
    extras,
    freeze_json,
    object_value,
    string_value,
    thaw_json,
)
from theater.frontend.dto.identity import Actor, TerminalIdentity
from theater.frontend.errors import ErrorValue


class OperationState(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    UNCERTAIN = "uncertain"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DispatchIdentity:
    terminal: TerminalIdentity | None = None
    backend_generation: int | None = None
    native_session_id: str | None = None
    native_turn_id: str | None = None
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))

    def to_wire(self) -> dict[str, object]:
        return append_extras(
            {
                "terminal": self.terminal.to_wire() if self.terminal is not None else None,
                "backend_generation": self.backend_generation,
                "native_session_id": self.native_session_id,
                "native_turn_id": self.native_turn_id,
            },
            self.extra,
        )

    @classmethod
    def from_wire(cls, value: object) -> DispatchIdentity:
        data = object_value(value, "dispatch identity")
        generation = data.get("backend_generation")
        if generation is not None and (type(generation) is not int or generation < 0):
            raise TypeError("dispatch identity.backend_generation must be non-negative")
        terminal = data.get("terminal")
        return cls(
            terminal=TerminalIdentity.from_wire(terminal) if terminal is not None else None,
            backend_generation=generation,
            native_session_id=string_value(
                data.get("native_session_id"), "dispatch identity.native_session_id", optional=True
            ),
            native_turn_id=string_value(
                data.get("native_turn_id"), "dispatch identity.native_turn_id", optional=True
            ),
            extra=extras(
                data,
                {"terminal", "backend_generation", "native_session_id", "native_turn_id"},
            ),
        )


@dataclass(frozen=True, slots=True)
class Operation:
    operation_id: str
    kind: str
    state: str
    phase: str
    actor: Actor
    target_ids: tuple[str, ...] = ()
    control_operation_id: str | None = None
    job_handle: str | None = None
    dispatch_identity: DispatchIdentity | None = None
    result: JSONValue = None
    error: ErrorValue | None = None
    created_at: float | None = None
    updated_at: float | None = None
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def known_state(self) -> OperationState | None:
        try:
            return OperationState(self.state)
        except ValueError:
            return None

    def to_wire(self) -> dict[str, object]:
        return append_extras(
            {
                "operation_id": self.operation_id,
                "kind": self.kind,
                "state": self.state,
                "phase": self.phase,
                "actor": self.actor.to_wire(),
                "target_ids": list(self.target_ids),
                "control_operation_id": self.control_operation_id,
                "job_handle": self.job_handle,
                "dispatch_identity": (
                    self.dispatch_identity.to_wire() if self.dispatch_identity is not None else None
                ),
                "result": thaw_json(self.result),
                "error": self.error.to_wire() if self.error is not None else None,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
            },
            self.extra,
        )

    @classmethod
    def from_wire(cls, value: object) -> Operation:
        data = object_value(value, "operation")
        raw_targets = data.get("target_ids", [])
        if not isinstance(raw_targets, (list, tuple)):
            raise TypeError("operation.target_ids must be an array")
        targets = tuple(string_value(item, "operation.target_ids[]") or "" for item in raw_targets)
        dispatch = data.get("dispatch_identity")
        error = data.get("error")
        return cls(
            operation_id=string_value(data.get("operation_id"), "operation.operation_id") or "",
            kind=string_value(data.get("kind"), "operation.kind") or "",
            state=string_value(data.get("state"), "operation.state") or "",
            phase=string_value(data.get("phase"), "operation.phase") or "",
            actor=Actor.from_wire(data.get("actor")),
            target_ids=targets,
            control_operation_id=string_value(
                data.get("control_operation_id"), "operation.control_operation_id", optional=True
            ),
            job_handle=string_value(data.get("job_handle"), "operation.job_handle", optional=True),
            dispatch_identity=(
                DispatchIdentity.from_wire(dispatch) if dispatch is not None else None
            ),
            result=freeze_json(data.get("result"), "operation.result"),
            error=ErrorValue.from_wire(error) if error is not None else None,
            created_at=_optional_number(data.get("created_at"), "operation.created_at"),
            updated_at=_optional_number(data.get("updated_at"), "operation.updated_at"),
            extra=extras(
                data,
                {
                    "operation_id",
                    "kind",
                    "state",
                    "phase",
                    "actor",
                    "target_ids",
                    "control_operation_id",
                    "job_handle",
                    "dispatch_identity",
                    "result",
                    "error",
                    "created_at",
                    "updated_at",
                },
            ),
        )


def _optional_number(value: object, label: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{label} must be a number or null")
    return float(value)


__all__ = ["DispatchIdentity", "Operation", "OperationState"]
