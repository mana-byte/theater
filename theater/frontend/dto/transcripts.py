"""Typed transcript reads, candidates, and binding results."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from theater.frontend.dto._wire import (
    JSONValue,
    boolean_value,
    extras,
    integer_value,
    object_value,
    string_value,
)


def _optional_number(value: object, label: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{label} must be a number or null")
    return float(value)


def _optional_integer(value: object, label: str) -> int | None:
    if value is None:
        return None
    return integer_value(value, label)


@dataclass(frozen=True, slots=True)
class TranscriptEvent:
    event_position: int
    index: int
    role: str
    text: str
    text_start_byte: int
    text_end_byte: int
    text_total_bytes: int
    reaches_text_start: bool
    tool_name: str | None = None
    turn_end: bool = False
    turn_terminal: bool = False
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def from_wire(cls, value: object) -> TranscriptEvent:
        data = object_value(value, "transcript event")
        return cls(
            event_position=integer_value(
                data.get("event_position"), "transcript event.event_position"
            ),
            index=integer_value(data.get("index"), "transcript event.index"),
            role=string_value(data.get("role"), "transcript event.role") or "",
            text=string_value(data.get("text"), "transcript event.text") or "",
            text_start_byte=integer_value(
                data.get("text_start_byte"), "transcript event.text_start_byte"
            ),
            text_end_byte=integer_value(
                data.get("text_end_byte"), "transcript event.text_end_byte"
            ),
            text_total_bytes=integer_value(
                data.get("text_total_bytes"), "transcript event.text_total_bytes"
            ),
            reaches_text_start=boolean_value(
                data.get("reaches_text_start"), "transcript event.reaches_text_start"
            ),
            tool_name=string_value(
                data.get("tool_name"), "transcript event.tool_name", optional=True
            ),
            turn_end=boolean_value(data.get("turn_end", False), "transcript event.turn_end"),
            turn_terminal=boolean_value(
                data.get("turn_terminal", False), "transcript event.turn_terminal"
            ),
            extra=extras(
                data,
                {
                    "event_position",
                    "index",
                    "role",
                    "text",
                    "text_start_byte",
                    "text_end_byte",
                    "text_total_bytes",
                    "reaches_text_start",
                    "tool_name",
                    "turn_end",
                    "turn_terminal",
                },
            ),
        )


@dataclass(frozen=True, slots=True)
class TranscriptReadPage:
    participant_id: str
    events: tuple[TranscriptEvent, ...]
    path: str | None
    cursor: str | None
    next_cursor: str | None
    has_more: bool
    truncated: bool
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def from_wire(cls, value: object) -> TranscriptReadPage:
        data = object_value(value, "transcript page")
        raw_events = data.get("events")
        if not isinstance(raw_events, (list, tuple)):
            raise TypeError("transcript page.events must be an array")
        return cls(
            participant_id=string_value(data.get("id"), "transcript page.id") or "",
            events=tuple(TranscriptEvent.from_wire(item) for item in raw_events),
            path=string_value(data.get("path"), "transcript page.path", optional=True),
            cursor=string_value(data.get("cursor"), "transcript page.cursor", optional=True),
            next_cursor=string_value(
                data.get("next_cursor"), "transcript page.next_cursor", optional=True
            ),
            has_more=boolean_value(data.get("has_more"), "transcript page.has_more"),
            truncated=boolean_value(data.get("truncated"), "transcript page.truncated"),
            extra=extras(
                data,
                {"id", "events", "path", "cursor", "next_cursor", "has_more", "truncated"},
            ),
        )


@dataclass(frozen=True, slots=True)
class TranscriptCandidate:
    location: str
    session_id: str | None = None
    mtime: float | None = None
    size: int | None = None
    provenance: str | None = None
    rejection_reason: str | None = None
    domain: str | None = None
    owner_id: str | None = None
    tombstone_id: str | None = None
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def from_wire(cls, value: object) -> TranscriptCandidate:
        data = object_value(value, "transcript candidate")
        return cls(
            location=string_value(data.get("location"), "transcript candidate.location") or "",
            session_id=string_value(
                data.get("session_id"), "transcript candidate.session_id", optional=True
            ),
            mtime=_optional_number(data.get("mtime"), "transcript candidate.mtime"),
            size=_optional_integer(data.get("size"), "transcript candidate.size"),
            provenance=string_value(
                data.get("provenance"), "transcript candidate.provenance", optional=True
            ),
            rejection_reason=string_value(
                data.get("rejection_reason"),
                "transcript candidate.rejection_reason",
                optional=True,
            ),
            domain=string_value(data.get("domain"), "transcript candidate.domain", optional=True),
            owner_id=string_value(data.get("owner"), "transcript candidate.owner", optional=True),
            tombstone_id=string_value(
                data.get("tombstone"), "transcript candidate.tombstone", optional=True
            ),
            extra=extras(
                data,
                {
                    "location",
                    "session_id",
                    "mtime",
                    "size",
                    "provenance",
                    "rejection_reason",
                    "domain",
                    "owner",
                    "tombstone",
                },
            ),
        )


@dataclass(frozen=True, slots=True)
class TranscriptBindResult:
    participant_id: str
    location: str
    session_id: str | None = None
    prior_owner_id: str | None = None
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def from_wire(cls, value: object) -> TranscriptBindResult:
        data = object_value(value, "transcript bind result")
        return cls(
            participant_id=string_value(
                data.get("participant_id"), "transcript bind result.participant_id"
            )
            or "",
            location=string_value(data.get("location"), "transcript bind result.location") or "",
            session_id=string_value(
                data.get("session_id"), "transcript bind result.session_id", optional=True
            ),
            prior_owner_id=string_value(
                data.get("prior_owner_id"),
                "transcript bind result.prior_owner_id",
                optional=True,
            ),
            extra=extras(
                data,
                {"participant_id", "location", "session_id", "prior_owner_id"},
            ),
        )


__all__ = [
    "TranscriptBindResult",
    "TranscriptCandidate",
    "TranscriptEvent",
    "TranscriptReadPage",
]
