"""Bounded immutable snapshot page values."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from theater.frontend.dto._wire import (
    JSONValue,
    append_extras,
    extras,
    integer_value,
    object_value,
    string_value,
)
from theater.frontend.dto.events import EventCursor
from theater.frontend.dto.jobs import Job
from theater.frontend.dto.operations import Operation
from theater.frontend.dto.participants import Participant
from theater.frontend.dto.providers import Provider
from theater.frontend.dto.workspaces import Workspace


@dataclass(frozen=True, slots=True)
class SnapshotPage:
    snapshot_id: str
    page: int
    complete: bool
    ending_cursor: EventCursor
    participants: tuple[Participant, ...] = ()
    operations: tuple[Operation, ...] = ()
    jobs: tuple[Job, ...] = ()
    providers: tuple[Provider, ...] = ()
    workspaces: tuple[Workspace, ...] = ()
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))

    def to_wire(self) -> dict[str, object]:
        return append_extras(
            {
                "snapshot_id": self.snapshot_id,
                "page": self.page,
                "complete": self.complete,
                "ending_cursor": self.ending_cursor.to_wire(),
                "participants": [item.to_wire() for item in self.participants],
                "operations": [item.to_wire() for item in self.operations],
                "jobs": [item.to_wire() for item in self.jobs],
                "providers": [item.to_wire() for item in self.providers],
                "workspaces": [item.to_wire() for item in self.workspaces],
            },
            self.extra,
        )

    @classmethod
    def from_wire(cls, value: object) -> SnapshotPage:
        data = object_value(value, "snapshot page")
        complete = data.get("complete")
        if type(complete) is not bool:
            raise TypeError("snapshot page.complete must be a boolean")
        return cls(
            snapshot_id=string_value(data.get("snapshot_id"), "snapshot page.snapshot_id") or "",
            page=integer_value(data.get("page"), "snapshot page.page"),
            complete=complete,
            ending_cursor=EventCursor.from_wire(data.get("ending_cursor")),
            participants=_items(data, "participants", Participant.from_wire),
            operations=_items(data, "operations", Operation.from_wire),
            jobs=_items(data, "jobs", Job.from_wire),
            providers=_items(data, "providers", Provider.from_wire),
            workspaces=_items(data, "workspaces", Workspace.from_wire),
            extra=extras(
                data,
                {
                    "snapshot_id",
                    "page",
                    "complete",
                    "ending_cursor",
                    "participants",
                    "operations",
                    "jobs",
                    "providers",
                    "workspaces",
                },
            ),
        )


def _items(data: Mapping[str, object], key: str, decoder):
    values = data.get(key, [])
    if not isinstance(values, (list, tuple)):
        raise TypeError(f"snapshot page.{key} must be an array")
    return tuple(decoder(item) for item in values)


__all__ = ["SnapshotPage"]
