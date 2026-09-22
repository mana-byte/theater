"""Public actors, ownership, and physical terminal identity."""

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
    integer_value,
    object_value,
    string_value,
    thaw_json,
)


class ControlOwnerKind(StrEnum):
    LOCAL_OPERATOR = "local_operator"
    PARTICIPANT = "participant"


@dataclass(frozen=True, slots=True)
class Actor:
    client_id: str
    participant_id: str | None = None

    def to_wire(self) -> dict[str, object]:
        return {"client_id": self.client_id, "participant_id": self.participant_id}

    @classmethod
    def from_wire(cls, value: object) -> Actor:
        data = object_value(value, "actor")
        return cls(
            client_id=string_value(data.get("client_id"), "actor.client_id") or "",
            participant_id=string_value(
                data.get("participant_id"), "actor.participant_id", optional=True
            ),
        )


@dataclass(frozen=True, slots=True)
class ControlOwner:
    kind: str
    revision: int
    participant_id: str | None = None
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def known_kind(self) -> ControlOwnerKind | None:
        try:
            return ControlOwnerKind(self.kind)
        except ValueError:
            return None

    def to_wire(self) -> dict[str, object]:
        return append_extras(
            {
                "kind": self.kind,
                "participant_id": self.participant_id,
                "revision": self.revision,
            },
            self.extra,
        )

    @classmethod
    def from_wire(cls, value: object) -> ControlOwner:
        data = object_value(value, "control owner")
        return cls(
            kind=string_value(data.get("kind"), "control owner.kind") or "",
            participant_id=string_value(
                data.get("participant_id"), "control owner.participant_id", optional=True
            ),
            revision=integer_value(data.get("revision"), "control owner.revision"),
            extra=extras(data, {"kind", "participant_id", "revision"}),
        )


@dataclass(frozen=True, slots=True)
class ProcessFacts:
    pid: int | None = None
    started_at: float | None = None
    executable: str | None = None
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))

    def to_wire(self) -> dict[str, object]:
        return append_extras(
            {"pid": self.pid, "started_at": self.started_at, "executable": self.executable},
            self.extra,
        )

    @classmethod
    def from_wire(cls, value: object) -> ProcessFacts:
        data = object_value(value, "process facts")
        raw_pid = data.get("pid")
        pid = None if raw_pid is None else integer_value(raw_pid, "process facts.pid", minimum=1)
        started = data.get("started_at")
        if started is not None and (
            not isinstance(started, (int, float)) or isinstance(started, bool)
        ):
            raise TypeError("process facts.started_at must be a number or null")
        executable = string_value(data.get("executable"), "process facts.executable", optional=True)
        return cls(
            pid=pid,
            started_at=float(started) if started is not None else None,
            executable=executable,
            extra=extras(data, {"pid", "started_at", "executable"}),
        )


@dataclass(frozen=True, slots=True)
class TerminalIdentity:
    provider_id: str
    provider_generation: int
    terminal_id: str
    terminal_incarnation: str
    occupant: Mapping[str, JSONValue]
    process: ProcessFacts | None = None
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))

    def to_wire(self) -> dict[str, object]:
        return append_extras(
            {
                "provider_id": self.provider_id,
                "provider_generation": self.provider_generation,
                "terminal_id": self.terminal_id,
                "terminal_incarnation": self.terminal_incarnation,
                "occupant": thaw_json(self.occupant),
                "process": self.process.to_wire() if self.process is not None else None,
            },
            self.extra,
        )

    @classmethod
    def from_wire(cls, value: object) -> TerminalIdentity:
        data = object_value(value, "terminal identity")
        occupant = freeze_json(object_value(data.get("occupant"), "terminal identity.occupant"))
        assert isinstance(occupant, Mapping)
        process = data.get("process")
        return cls(
            provider_id=string_value(data.get("provider_id"), "terminal identity.provider_id")
            or "",
            provider_generation=integer_value(
                data.get("provider_generation"), "terminal identity.provider_generation"
            ),
            terminal_id=string_value(data.get("terminal_id"), "terminal identity.terminal_id")
            or "",
            terminal_incarnation=string_value(
                data.get("terminal_incarnation"), "terminal identity.terminal_incarnation"
            )
            or "",
            occupant=occupant,
            process=ProcessFacts.from_wire(process) if process is not None else None,
            extra=extras(
                data,
                {
                    "provider_id",
                    "provider_generation",
                    "terminal_id",
                    "terminal_incarnation",
                    "occupant",
                    "process",
                },
            ),
        )


__all__ = ["Actor", "ControlOwner", "ControlOwnerKind", "ProcessFacts", "TerminalIdentity"]
