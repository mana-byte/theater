"""Public agent-job values."""

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
from theater.frontend.dto.identity import Actor
from theater.frontend.errors import ErrorValue


class JobState(StrEnum):
    RUNNING = "running"
    DONE = "done"
    CRASHED = "crashed"
    KILLED = "killed"


@dataclass(frozen=True, slots=True)
class Job:
    handle: str
    state: str
    kind: str
    actor: Actor | None = None
    legacy_caller_id: str | None = None
    target_id: str | None = None
    result: JSONValue = None
    error: ErrorValue | None = None
    created_at: float | None = None
    finished_at: float | None = None
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def known_state(self) -> JobState | None:
        try:
            return JobState(self.state)
        except ValueError:
            return None

    def to_wire(self) -> dict[str, object]:
        return append_extras(
            {
                "handle": self.handle,
                "state": self.state,
                "kind": self.kind,
                "actor": self.actor.to_wire() if self.actor is not None else None,
                "legacy_caller_id": self.legacy_caller_id,
                "target_id": self.target_id,
                "result": thaw_json(self.result),
                "error": self.error.to_wire() if self.error is not None else None,
                "created_at": self.created_at,
                "finished_at": self.finished_at,
            },
            self.extra,
        )

    @classmethod
    def from_wire(cls, value: object) -> Job:
        data = object_value(value, "job")
        error = data.get("error")
        actor = data.get("actor")
        target = data.get("target_id")
        if target is not None and not isinstance(target, str):
            raise TypeError("job.target_id must be a string or null")
        return cls(
            handle=string_value(data.get("handle"), "job.handle") or "",
            state=string_value(data.get("state"), "job.state") or "",
            kind=string_value(data.get("kind"), "job.kind") or "",
            actor=Actor.from_wire(actor) if actor is not None else None,
            legacy_caller_id=string_value(
                data.get("legacy_caller_id"), "job.legacy_caller_id", optional=True
            ),
            target_id=target,
            result=freeze_json(data.get("result"), "job.result"),
            error=ErrorValue.from_wire(error) if error is not None else None,
            created_at=_optional_number(data.get("created_at"), "job.created_at"),
            finished_at=_optional_number(data.get("finished_at"), "job.finished_at"),
            extra=extras(
                data,
                {
                    "handle",
                    "state",
                    "kind",
                    "actor",
                    "legacy_caller_id",
                    "target_id",
                    "result",
                    "error",
                    "created_at",
                    "finished_at",
                },
            ),
        )


def _optional_number(value: object, label: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{label} must be a number or null")
    return float(value)


__all__ = ["Job", "JobState"]
