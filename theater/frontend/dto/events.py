"""Orchestration journal values applied atomically by clients."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
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

EVENT_KINDS = (
    "participant.updated",
    "participant.removed",
    "participant.controls_changed",
    "participant.owner_changed",
    "provider.updated",
    "terminal.binding_changed",
    "operation.updated",
    "job.updated",
    "job.removed",
    "workspace.updated",
    "workspace.usage_changed",
    "catalog.invalidated",
)


@dataclass(frozen=True, slots=True)
class EventCursor:
    stream_id: str
    sequence: int

    def to_wire(self) -> dict[str, object]:
        return {"stream_id": self.stream_id, "sequence": self.sequence}

    @classmethod
    def from_wire(cls, value: object) -> EventCursor:
        data = object_value(value, "event cursor")
        return cls(
            stream_id=string_value(data.get("stream_id"), "event cursor.stream_id") or "",
            sequence=integer_value(data.get("sequence"), "event cursor.sequence"),
        )


@dataclass(frozen=True, slots=True)
class Event:
    kind: str
    entity_id: str
    entity_revision: int
    payload: Mapping[str, JSONValue]
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def known_kind(self) -> bool:
        return self.kind in EVENT_KINDS

    def to_wire(self) -> dict[str, object]:
        return append_extras(
            {
                "kind": self.kind,
                "entity_id": self.entity_id,
                "entity_revision": self.entity_revision,
                "payload": thaw_json(self.payload),
            },
            self.extra,
        )

    @classmethod
    def from_wire(cls, value: object) -> Event:
        data = object_value(value, "event")
        payload = freeze_json(object_value(data.get("payload"), "event.payload"))
        assert isinstance(payload, Mapping)
        return cls(
            kind=string_value(data.get("kind"), "event.kind") or "",
            entity_id=string_value(data.get("entity_id"), "event.entity_id") or "",
            entity_revision=integer_value(data.get("entity_revision"), "event.entity_revision"),
            payload=payload,
            extra=extras(data, {"kind", "entity_id", "entity_revision", "payload"}),
        )


@dataclass(frozen=True, slots=True)
class EventTransaction:
    transaction_id: str
    events: tuple[Event, ...]
    ending_cursor: EventCursor
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))

    def to_wire(self) -> dict[str, object]:
        return append_extras(
            {
                "transaction_id": self.transaction_id,
                "events": [event.to_wire() for event in self.events],
                "ending_cursor": self.ending_cursor.to_wire(),
            },
            self.extra,
        )

    @classmethod
    def from_wire(cls, value: object) -> EventTransaction:
        data = object_value(value, "event transaction")
        raw_events = data.get("events")
        if not isinstance(raw_events, (list, tuple)) or not raw_events:
            raise TypeError("event transaction.events must be a non-empty array")
        return cls(
            transaction_id=string_value(
                data.get("transaction_id"), "event transaction.transaction_id"
            )
            or "",
            events=tuple(Event.from_wire(item) for item in raw_events),
            ending_cursor=EventCursor.from_wire(data.get("ending_cursor")),
            extra=extras(data, {"transaction_id", "events", "ending_cursor"}),
        )


__all__ = ["EVENT_KINDS", "Event", "EventCursor", "EventTransaction"]
