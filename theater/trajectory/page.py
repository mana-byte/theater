"""Trajectory page, grouping, coverage, and update values."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Self

from theater.trajectory.content import sanitize_text
from theater.trajectory.enums import GroupKind, PanelState, TrajectoryValidationError
from theater.trajectory.records import TrajectoryRecord
from theater.trajectory.validation import (
    boolean,
    enum_value,
    keys,
    mapping,
    sequence,
    string,
    string_or_none,
)


@dataclass(frozen=True, slots=True)
class TrajectoryGroup:
    group_id: str
    kind: GroupKind
    label: str
    record_ids: tuple[str, ...] = ()
    children: tuple[Self, ...] = ()
    turn_id: str | None = None
    step_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.group_id, str) or not self.group_id:
            raise TrajectoryValidationError("group_id must be a non-empty string")
        if not isinstance(self.label, str):
            raise TrajectoryValidationError("group label must be a string")
        object.__setattr__(self, "kind", enum_value(GroupKind, self.kind, "group.kind"))
        object.__setattr__(self, "label", sanitize_text(self.label))
        object.__setattr__(self, "record_ids", tuple(self.record_ids))
        object.__setattr__(self, "children", tuple(self.children))
        if any(not isinstance(value, str) or not value for value in self.record_ids):
            raise TrajectoryValidationError("group.record_ids must contain non-empty strings")
        if any(not isinstance(value, TrajectoryGroup) for value in self.children):
            raise TrajectoryValidationError("group.children must contain TrajectoryGroup values")
        string_or_none(self.turn_id, "group.turn_id")
        string_or_none(self.step_id, "group.step_id")

    def to_wire(self) -> dict[str, object]:
        return {
            "group_id": self.group_id,
            "kind": self.kind.value,
            "label": self.label,
            "record_ids": list(self.record_ids),
            "children": [child.to_wire() for child in self.children],
            "turn_id": self.turn_id,
            "step_id": self.step_id,
        }

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = mapping(value, "trajectory group")
        keys(
            data,
            required={"group_id", "kind", "label"},
            optional={"record_ids", "children", "turn_id", "step_id"},
            label="trajectory group",
        )
        return cls(
            group_id=string(data["group_id"], "group.group_id"),
            kind=enum_value(GroupKind, data["kind"], "group.kind"),
            label=string(data["label"], "group.label"),
            record_ids=tuple(
                string(item, "group.record_ids[]")
                for item in sequence(data.get("record_ids", []), "group.record_ids")
            ),
            children=tuple(
                TrajectoryGroup.from_wire(item)
                for item in sequence(data.get("children", []), "group.children")
            ),
            turn_id=string_or_none(data.get("turn_id"), "group.turn_id"),
            step_id=string_or_none(data.get("step_id"), "group.step_id"),
        )


@dataclass(frozen=True, slots=True)
class CoverageGap:
    stream: str
    reason: str
    start: str | None = None
    end: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.stream, str) or not self.stream:
            raise TrajectoryValidationError("coverage gap stream must be a non-empty string")
        if not isinstance(self.reason, str) or not self.reason:
            raise TrajectoryValidationError("coverage gap reason must be a non-empty string")
        object.__setattr__(self, "reason", sanitize_text(self.reason))
        string_or_none(self.start, "coverage gap.start")
        string_or_none(self.end, "coverage gap.end")

    def to_wire(self) -> dict[str, object]:
        return {"stream": self.stream, "reason": self.reason, "start": self.start, "end": self.end}

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = mapping(value, "coverage gap")
        keys(data, required={"stream", "reason"}, optional={"start", "end"}, label="coverage gap")
        return cls(
            stream=string(data["stream"], "gap.stream"),
            reason=string(data["reason"], "gap.reason"),
            start=string_or_none(data.get("start"), "gap.start"),
            end=string_or_none(data.get("end"), "gap.end"),
        )


@dataclass(frozen=True, slots=True)
class TrajectoryCoverage:
    transcript_floor: str | None = None
    theater_floor: str | None = None
    gaps: tuple[CoverageGap, ...] = ()

    def __post_init__(self) -> None:
        string_or_none(self.transcript_floor, "coverage.transcript_floor")
        string_or_none(self.theater_floor, "coverage.theater_floor")
        object.__setattr__(self, "gaps", tuple(self.gaps))
        if any(not isinstance(gap, CoverageGap) for gap in self.gaps):
            raise TrajectoryValidationError("coverage.gaps must contain CoverageGap values")

    def to_wire(self) -> dict[str, object]:
        return {
            "transcript_floor": self.transcript_floor,
            "theater_floor": self.theater_floor,
            "gaps": [gap.to_wire() for gap in self.gaps],
        }

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = mapping(value, "trajectory coverage")
        keys(
            data,
            required=set(),
            optional={"transcript_floor", "theater_floor", "gaps"},
            label="trajectory coverage",
        )
        return cls(
            transcript_floor=string_or_none(
                data.get("transcript_floor"), "coverage.transcript_floor"
            ),
            theater_floor=string_or_none(data.get("theater_floor"), "coverage.theater_floor"),
            gaps=tuple(
                CoverageGap.from_wire(item)
                for item in sequence(data.get("gaps", []), "coverage.gaps")
            ),
        )


@dataclass(frozen=True, slots=True)
class PanelStateInfo:
    state: PanelState
    message: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", enum_value(PanelState, self.state, "panel state"))
        object.__setattr__(self, "message", sanitize_text(self.message))

    def to_wire(self) -> dict[str, object]:
        return {"state": self.state.value, "message": self.message}

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = mapping(value, "panel state")
        keys(data, required={"state"}, optional={"message"}, label="panel state")
        return cls(
            state=enum_value(PanelState, data["state"], "panel state.state"),
            message=string(data.get("message", ""), "panel state.message"),
        )


@dataclass(frozen=True, slots=True)
class TrajectoryPage:
    panel_state: PanelStateInfo
    stream_id: str | None = None
    cursor: str | None = None
    records: tuple[TrajectoryRecord, ...] = ()
    groups: tuple[TrajectoryGroup, ...] = ()
    older_cursor: str | None = None
    has_older: bool = False
    coverage: TrajectoryCoverage = field(default_factory=TrajectoryCoverage)
    truncated_by_bytes: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.panel_state, PanelStateInfo):
            raise TrajectoryValidationError("page.panel_state must be PanelStateInfo")
        string_or_none(self.stream_id, "page.stream_id")
        string_or_none(self.cursor, "page.cursor")
        string_or_none(self.older_cursor, "page.older_cursor")
        object.__setattr__(self, "records", tuple(self.records))
        object.__setattr__(self, "groups", tuple(self.groups))
        if any(not isinstance(record, TrajectoryRecord) for record in self.records):
            raise TrajectoryValidationError("page.records must contain TrajectoryRecord values")
        if any(not isinstance(group, TrajectoryGroup) for group in self.groups):
            raise TrajectoryValidationError("page.groups must contain TrajectoryGroup values")
        if not isinstance(self.coverage, TrajectoryCoverage):
            raise TrajectoryValidationError("page.coverage must be TrajectoryCoverage")
        if type(self.has_older) is not bool or type(self.truncated_by_bytes) is not bool:
            raise TrajectoryValidationError("page boolean fields must be booleans")

    @property
    def state(self) -> PanelState:
        return self.panel_state.state

    def to_wire(self) -> dict[str, object]:
        return {
            "panel_state": self.panel_state.to_wire(),
            "stream_id": self.stream_id,
            "cursor": self.cursor,
            "records": [record.to_wire() for record in self.records],
            "groups": [group.to_wire() for group in self.groups],
            "older_cursor": self.older_cursor,
            "has_older": self.has_older,
            "coverage": self.coverage.to_wire(),
            "truncated_by_bytes": self.truncated_by_bytes,
        }

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = mapping(value, "trajectory page")
        keys(
            data,
            required={"panel_state"},
            optional={
                "stream_id",
                "cursor",
                "records",
                "groups",
                "older_cursor",
                "has_older",
                "coverage",
                "truncated_by_bytes",
            },
            label="trajectory page",
        )
        return cls(
            panel_state=PanelStateInfo.from_wire(data["panel_state"]),
            stream_id=string_or_none(data.get("stream_id"), "page.stream_id"),
            cursor=string_or_none(data.get("cursor"), "page.cursor"),
            records=tuple(
                TrajectoryRecord.from_wire(item)
                for item in sequence(data.get("records", []), "page.records")
            ),
            groups=tuple(
                TrajectoryGroup.from_wire(item)
                for item in sequence(data.get("groups", []), "page.groups")
            ),
            older_cursor=string_or_none(data.get("older_cursor"), "page.older_cursor"),
            has_older=boolean(data.get("has_older", False), "page.has_older"),
            coverage=TrajectoryCoverage.from_wire(data.get("coverage", {})),
            truncated_by_bytes=boolean(
                data.get("truncated_by_bytes", False), "page.truncated_by_bytes"
            ),
        )


@dataclass(frozen=True, slots=True)
class TrajectoryUpsert:
    record: TrajectoryRecord

    def __post_init__(self) -> None:
        if not isinstance(self.record, TrajectoryRecord):
            raise TrajectoryValidationError("upsert.record must be TrajectoryRecord")

    def to_wire(self) -> dict[str, object]:
        return {"record": self.record.to_wire()}

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = mapping(value, "trajectory upsert")
        keys(data, required={"record"}, optional=set(), label="trajectory upsert")
        return cls(record=TrajectoryRecord.from_wire(data["record"]))


@dataclass(frozen=True, slots=True)
class TrajectoryDelta:
    stream_id: str
    cursor: str | None = None
    upserts: tuple[TrajectoryUpsert, ...] = ()
    resync_required: bool = False
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.stream_id, str) or not self.stream_id:
            raise TrajectoryValidationError("delta.stream_id must be a non-empty string")
        string_or_none(self.cursor, "delta.cursor")
        object.__setattr__(self, "upserts", tuple(self.upserts))
        if any(not isinstance(upsert, TrajectoryUpsert) for upsert in self.upserts):
            raise TrajectoryValidationError("delta.upserts must contain TrajectoryUpsert values")
        if type(self.resync_required) is not bool:
            raise TrajectoryValidationError("delta.resync_required must be a boolean")
        if self.reason is not None and not isinstance(self.reason, str):
            raise TrajectoryValidationError("delta.reason must be a string or null")
        if self.reason is not None:
            object.__setattr__(self, "reason", sanitize_text(self.reason))

    def to_wire(self) -> dict[str, object]:
        return {
            "stream_id": self.stream_id,
            "cursor": self.cursor,
            "upserts": [upsert.to_wire() for upsert in self.upserts],
            "resync_required": self.resync_required,
            "reason": self.reason,
        }

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = mapping(value, "trajectory delta")
        keys(
            data,
            required={"stream_id"},
            optional={"cursor", "upserts", "resync_required", "reason"},
            label="trajectory delta",
        )
        return cls(
            stream_id=string(data["stream_id"], "delta.stream_id"),
            cursor=string_or_none(data.get("cursor"), "delta.cursor"),
            upserts=tuple(
                TrajectoryUpsert.from_wire(item)
                for item in sequence(data.get("upserts", []), "delta.upserts")
            ),
            resync_required=boolean(data.get("resync_required", False), "delta.resync_required"),
            reason=string_or_none(data.get("reason"), "delta.reason"),
        )


__all__ = [
    "CoverageGap",
    "PanelStateInfo",
    "TrajectoryCoverage",
    "TrajectoryDelta",
    "TrajectoryGroup",
    "TrajectoryPage",
    "TrajectoryUpsert",
]
