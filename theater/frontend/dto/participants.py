"""Participant and per-action capability projections."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from theater.frontend.dto._wire import (
    JSONValue,
    append_extras,
    boolean_value,
    extras,
    freeze_json,
    integer_value,
    object_value,
    string_value,
    thaw_json,
)
from theater.frontend.dto.identity import ControlOwner, TerminalIdentity


def _optional_number(value: object, label: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{label} must be a number or null")
    return float(value)


@dataclass(frozen=True, slots=True)
class TranscriptIdentity:
    """Authoritative public transcript identity and its current trust state."""

    state: str
    session_id: str | None = None
    provenance: str | None = None
    location: str | None = None
    domain: str | None = None
    detail: str | None = None
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def from_wire(cls, value: object) -> TranscriptIdentity:
        data = object_value(value, "transcript identity")
        return cls(
            state=string_value(data.get("state"), "transcript identity.state") or "",
            session_id=string_value(
                data.get("session_id"), "transcript identity.session_id", optional=True
            ),
            provenance=string_value(
                data.get("provenance"), "transcript identity.provenance", optional=True
            ),
            location=string_value(
                data.get("location"), "transcript identity.location", optional=True
            ),
            domain=string_value(data.get("domain"), "transcript identity.domain", optional=True),
            detail=string_value(data.get("detail"), "transcript identity.detail", optional=True),
            extra=extras(
                data,
                {"state", "session_id", "provenance", "location", "domain", "detail"},
            ),
        )

    def to_wire(self) -> dict[str, object]:
        return append_extras(
            {
                "state": self.state,
                "session_id": self.session_id,
                "provenance": self.provenance,
                "location": self.location,
                "domain": self.domain,
                "detail": self.detail,
            },
            self.extra,
        )


@dataclass(frozen=True, slots=True)
class ResumeCandidate:
    """One recently dead participant with an authoritative resume verdict."""

    participant_id: str
    harness: str
    resume_state: str
    identity: TranscriptIdentity
    cwd: str | None = None
    name: str | None = None
    description: str | None = None
    spawn_prompt: str | None = None
    last_activity: float | None = None
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def from_wire(cls, value: object) -> ResumeCandidate:
        data = object_value(value, "resume candidate")
        return cls(
            participant_id=string_value(
                data.get("participant_id"), "resume candidate.participant_id"
            )
            or "",
            harness=string_value(data.get("harness"), "resume candidate.harness") or "",
            resume_state=string_value(data.get("resume_state"), "resume candidate.resume_state")
            or "",
            identity=TranscriptIdentity.from_wire(data.get("transcript_identity")),
            cwd=string_value(data.get("cwd"), "resume candidate.cwd", optional=True),
            name=string_value(data.get("name"), "resume candidate.name", optional=True),
            description=string_value(
                data.get("description"), "resume candidate.description", optional=True
            ),
            spawn_prompt=string_value(
                data.get("spawn_prompt"), "resume candidate.spawn_prompt", optional=True
            ),
            last_activity=_optional_number(
                data.get("last_activity"), "resume candidate.last_activity"
            ),
            extra=extras(
                data,
                {
                    "participant_id",
                    "harness",
                    "resume_state",
                    "transcript_identity",
                    "cwd",
                    "name",
                    "description",
                    "spawn_prompt",
                    "last_activity",
                },
            ),
        )

    def to_wire(self) -> dict[str, object]:
        return append_extras(
            {
                "participant_id": self.participant_id,
                "harness": self.harness,
                "resume_state": self.resume_state,
                "transcript_identity": self.identity.to_wire(),
                "cwd": self.cwd,
                "name": self.name,
                "description": self.description,
                "spawn_prompt": self.spawn_prompt,
                "last_activity": self.last_activity,
            },
            self.extra,
        )


@dataclass(frozen=True, slots=True)
class ActionCapability:
    supported: bool
    route_available: bool
    admissible: bool
    reason: str | None = None
    detail: str | None = None
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))

    def to_wire(self) -> dict[str, object]:
        return append_extras(
            {
                "supported": self.supported,
                "route_available": self.route_available,
                "admissible": self.admissible,
                "reason": self.reason,
                "detail": self.detail,
            },
            self.extra,
        )

    @classmethod
    def from_wire(cls, value: object) -> ActionCapability:
        data = object_value(value, "action capability")
        return cls(
            supported=boolean_value(data.get("supported"), "action capability.supported"),
            route_available=boolean_value(
                data.get("route_available"), "action capability.route_available"
            ),
            admissible=boolean_value(data.get("admissible"), "action capability.admissible"),
            reason=string_value(data.get("reason"), "action capability.reason", optional=True),
            detail=string_value(data.get("detail"), "action capability.detail", optional=True),
            extra=extras(data, {"supported", "route_available", "admissible", "reason", "detail"}),
        )


@dataclass(frozen=True, slots=True)
class Controls:
    actions: Mapping[str, ActionCapability]
    revision: int
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def from_wire(cls, value: object) -> Controls:
        data = object_value(value, "controls")
        raw_actions = object_value(data.get("actions"), "controls.actions")
        return cls(
            actions=MappingProxyType(
                {key: ActionCapability.from_wire(item) for key, item in raw_actions.items()}
            ),
            revision=integer_value(data.get("revision"), "controls.revision"),
            extra=extras(data, {"actions", "revision"}),
        )

    def to_wire(self) -> dict[str, object]:
        return append_extras(
            {
                "actions": {key: item.to_wire() for key, item in self.actions.items()},
                "revision": self.revision,
            },
            self.extra,
        )


@dataclass(frozen=True, slots=True)
class NativeRouteSummary:
    backend_generation: int
    health: str
    native_session_id: str | None = None
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))

    def to_wire(self) -> dict[str, object]:
        return append_extras(
            {
                "backend_generation": self.backend_generation,
                "health": self.health,
                "native_session_id": self.native_session_id,
            },
            self.extra,
        )

    @classmethod
    def from_wire(cls, value: object) -> NativeRouteSummary:
        data = object_value(value, "native route")
        return cls(
            backend_generation=integer_value(
                data.get("backend_generation"), "native route.backend_generation"
            ),
            health=string_value(data.get("health"), "native route.health") or "",
            native_session_id=string_value(
                data.get("native_session_id"), "native route.native_session_id", optional=True
            ),
            extra=extras(data, {"backend_generation", "health", "native_session_id"}),
        )


@dataclass(frozen=True, slots=True)
class TerminalRouteSummary:
    identity: TerminalIdentity
    health: str
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))

    def to_wire(self) -> dict[str, object]:
        return append_extras(
            {"identity": self.identity.to_wire(), "health": self.health}, self.extra
        )

    @classmethod
    def from_wire(cls, value: object) -> TerminalRouteSummary:
        data = object_value(value, "terminal route")
        return cls(
            identity=TerminalIdentity.from_wire(data.get("identity")),
            health=string_value(data.get("health"), "terminal route.health") or "",
            extra=extras(data, {"identity", "health"}),
        )


@dataclass(frozen=True, slots=True)
class Participant:
    participant_id: str
    origin: str
    harness: str
    status: str
    owner: ControlOwner
    parent_id: str | None = None
    cwd: str | None = None
    workspace_id: str | None = None
    name: str | None = None
    description: str | None = None
    addressable: bool = False
    presence: str = "unknown"
    terminal_route: TerminalRouteSummary | None = None
    native_route: NativeRouteSummary | None = None
    trusted_identity: Mapping[str, JSONValue] | None = None
    transcript_identity: TranscriptIdentity | None = None
    resume_state: str | None = None
    created_at: float | None = None
    last_activity: float | None = None
    actions: Mapping[str, ActionCapability] = field(default_factory=lambda: MappingProxyType({}))
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def from_wire(cls, value: object) -> Participant:
        data = object_value(value, "participant")
        raw_actions = object_value(data.get("actions", {}), "participant.actions")
        actions = MappingProxyType(
            {key: ActionCapability.from_wire(item) for key, item in raw_actions.items()}
        )
        terminal = data.get("terminal_route")
        native = data.get("native_route")
        identity = data.get("trusted_identity")
        transcript_identity = data.get("transcript_identity")
        trusted_identity = None
        if identity is not None:
            frozen_identity = freeze_json(
                object_value(identity, "participant.trusted_identity"),
                "participant.trusted_identity",
            )
            assert isinstance(frozen_identity, Mapping)
            trusted_identity = frozen_identity
        return cls(
            participant_id=string_value(data.get("participant_id"), "participant.id") or "",
            origin=string_value(data.get("origin"), "participant.origin") or "",
            harness=string_value(data.get("harness"), "participant.harness") or "",
            status=string_value(data.get("status"), "participant.status") or "",
            owner=ControlOwner.from_wire(data.get("owner")),
            parent_id=string_value(data.get("parent_id"), "participant.parent_id", optional=True),
            cwd=string_value(data.get("cwd"), "participant.cwd", optional=True),
            workspace_id=string_value(
                data.get("workspace_id"), "participant.workspace_id", optional=True
            ),
            name=string_value(data.get("name"), "participant.name", optional=True),
            description=string_value(
                data.get("description"), "participant.description", optional=True
            ),
            addressable=boolean_value(data.get("addressable", False), "participant.addressable"),
            presence=string_value(data.get("presence", "unknown"), "participant.presence")
            or "unknown",
            terminal_route=TerminalRouteSummary.from_wire(terminal) if terminal else None,
            native_route=NativeRouteSummary.from_wire(native) if native else None,
            trusted_identity=trusted_identity,
            transcript_identity=(
                TranscriptIdentity.from_wire(transcript_identity)
                if transcript_identity is not None
                else None
            ),
            resume_state=string_value(
                data.get("resume_state"), "participant.resume_state", optional=True
            ),
            created_at=_optional_number(data.get("created_at"), "participant.created_at"),
            last_activity=_optional_number(data.get("last_activity"), "participant.last_activity"),
            actions=actions,
            extra=extras(
                data,
                {
                    "participant_id",
                    "origin",
                    "harness",
                    "status",
                    "owner",
                    "parent_id",
                    "cwd",
                    "workspace_id",
                    "name",
                    "description",
                    "addressable",
                    "presence",
                    "terminal_route",
                    "native_route",
                    "trusted_identity",
                    "transcript_identity",
                    "resume_state",
                    "created_at",
                    "last_activity",
                    "actions",
                },
            ),
        )

    def to_wire(self) -> dict[str, object]:
        return append_extras(
            {
                "participant_id": self.participant_id,
                "origin": self.origin,
                "harness": self.harness,
                "status": self.status,
                "owner": self.owner.to_wire(),
                "parent_id": self.parent_id,
                "cwd": self.cwd,
                "workspace_id": self.workspace_id,
                "name": self.name,
                "description": self.description,
                "addressable": self.addressable,
                "presence": self.presence,
                "terminal_route": (
                    self.terminal_route.to_wire() if self.terminal_route is not None else None
                ),
                "native_route": (
                    self.native_route.to_wire() if self.native_route is not None else None
                ),
                "trusted_identity": (
                    thaw_json(self.trusted_identity) if self.trusted_identity is not None else None
                ),
                "transcript_identity": (
                    self.transcript_identity.to_wire()
                    if self.transcript_identity is not None
                    else None
                ),
                "resume_state": self.resume_state,
                "created_at": self.created_at,
                "last_activity": self.last_activity,
                "actions": {key: item.to_wire() for key, item in self.actions.items()},
            },
            self.extra,
        )


__all__ = [
    "ActionCapability",
    "Controls",
    "NativeRouteSummary",
    "Participant",
    "ResumeCandidate",
    "TerminalRouteSummary",
    "TranscriptIdentity",
]
