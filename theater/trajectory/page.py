"""Trajectory page, grouping, coverage, and update values."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Self

from theater.constants.trajectory import (
    TRAJECTORY_CURSOR_MAX_BYTES,
    TRAJECTORY_IDENTIFIER_MAX_BYTES,
    TRAJECTORY_MAX_COVERAGE_GAPS,
    TRAJECTORY_MAX_GROUP_CHILDREN,
    TRAJECTORY_MAX_GROUP_RECORD_IDS,
    TRAJECTORY_MAX_PAGE_GROUPS,
    TRAJECTORY_PAGE_RECORD_LIMIT,
    TRAJECTORY_SOURCE_MAX_BYTES,
)
from theater.trajectory.content import ContentPreview, bounded_text
from theater.trajectory.enums import (
    GroupKind,
    PanelState,
    TrajectoryParticipantState,
    TrajectoryValidationError,
)
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
        object.__setattr__(
            self,
            "group_id",
            bounded_text(
                self.group_id,
                max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                label="group_id",
                nonempty=True,
            ),
        )
        object.__setattr__(self, "kind", enum_value(GroupKind, self.kind, "group.kind"))
        object.__setattr__(self, "label", ContentPreview.from_text(self.label).text)
        object.__setattr__(
            self,
            "record_ids",
            tuple(
                bounded_text(
                    value,
                    max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                    label="group.record_ids[]",
                    nonempty=True,
                )
                for value in self.record_ids
            ),
        )
        object.__setattr__(self, "children", tuple(self.children))
        if len(self.record_ids) > TRAJECTORY_MAX_GROUP_RECORD_IDS:
            raise TrajectoryValidationError(
                f"group.record_ids exceeds {TRAJECTORY_MAX_GROUP_RECORD_IDS} values"
            )
        if len(self.children) > TRAJECTORY_MAX_GROUP_CHILDREN:
            raise TrajectoryValidationError(
                f"group.children exceeds {TRAJECTORY_MAX_GROUP_CHILDREN} values"
            )
        if any(not isinstance(value, TrajectoryGroup) for value in self.children):
            raise TrajectoryValidationError("group.children must contain TrajectoryGroup values")
        for name in ("turn_id", "step_id"):
            value = string_or_none(getattr(self, name), f"group.{name}")
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    bounded_text(
                        value,
                        max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                        label=f"group.{name}",
                        nonempty=True,
                    ),
                )

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
        object.__setattr__(
            self,
            "stream",
            bounded_text(
                self.stream,
                max_bytes=TRAJECTORY_SOURCE_MAX_BYTES,
                label="coverage gap stream",
                nonempty=True,
            ),
        )
        reason = ContentPreview.from_text(self.reason).text
        if not reason:
            raise TrajectoryValidationError("coverage gap reason must be a non-empty string")
        object.__setattr__(self, "reason", reason)
        for name in ("start", "end"):
            value = string_or_none(getattr(self, name), f"coverage gap.{name}")
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    bounded_text(
                        value,
                        max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                        label=f"coverage gap.{name}",
                        nonempty=True,
                    ),
                )

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
        limits = {
            "transcript_floor": TRAJECTORY_CURSOR_MAX_BYTES,
            "theater_floor": TRAJECTORY_IDENTIFIER_MAX_BYTES,
        }
        for name, max_bytes in limits.items():
            value = string_or_none(getattr(self, name), f"coverage.{name}")
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    bounded_text(
                        value,
                        max_bytes=max_bytes,
                        label=f"coverage.{name}",
                        nonempty=True,
                    ),
                )
        object.__setattr__(self, "gaps", tuple(self.gaps))
        if len(self.gaps) > TRAJECTORY_MAX_COVERAGE_GAPS:
            raise TrajectoryValidationError(
                f"coverage.gaps exceeds {TRAJECTORY_MAX_COVERAGE_GAPS} values"
            )
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
    participant_state: TrajectoryParticipantState = TrajectoryParticipantState.UNKNOWN

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", enum_value(PanelState, self.state, "panel state"))
        object.__setattr__(self, "message", ContentPreview.from_text(self.message).text)
        object.__setattr__(
            self,
            "participant_state",
            enum_value(
                TrajectoryParticipantState,
                self.participant_state,
                "panel participant_state",
            ),
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "message": self.message,
            "participant_state": self.participant_state.value,
        }

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = mapping(value, "panel state")
        keys(
            data,
            required={"state"},
            optional={"message", "participant_state"},
            label="panel state",
        )
        return cls(
            state=enum_value(PanelState, data["state"], "panel state.state"),
            message=string(data.get("message", ""), "panel state.message"),
            participant_state=enum_value(
                TrajectoryParticipantState,
                data.get("participant_state", TrajectoryParticipantState.UNKNOWN.value),
                "panel participant_state",
            ),
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
        for name in ("stream_id", "cursor", "older_cursor"):
            value = string_or_none(getattr(self, name), f"page.{name}")
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    bounded_text(
                        value,
                        max_bytes=(
                            TRAJECTORY_CURSOR_MAX_BYTES
                            if name in {"cursor", "older_cursor"}
                            else TRAJECTORY_IDENTIFIER_MAX_BYTES
                        ),
                        label=f"page.{name}",
                        nonempty=True,
                    ),
                )
        object.__setattr__(self, "records", tuple(self.records))
        object.__setattr__(self, "groups", tuple(self.groups))
        if len(self.records) > TRAJECTORY_PAGE_RECORD_LIMIT:
            raise TrajectoryValidationError(
                f"page.records exceeds {TRAJECTORY_PAGE_RECORD_LIMIT} values"
            )
        if len(self.groups) > TRAJECTORY_MAX_PAGE_GROUPS:
            raise TrajectoryValidationError(
                f"page.groups exceeds {TRAJECTORY_MAX_PAGE_GROUPS} values"
            )
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
    panel_state: PanelStateInfo | None = None
    resync_required: bool = False
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "stream_id",
            bounded_text(
                self.stream_id,
                max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                label="delta.stream_id",
                nonempty=True,
            ),
        )
        if self.cursor is not None:
            object.__setattr__(
                self,
                "cursor",
                bounded_text(
                    self.cursor,
                    max_bytes=TRAJECTORY_CURSOR_MAX_BYTES,
                    label="delta.cursor",
                    nonempty=True,
                ),
            )
        object.__setattr__(self, "upserts", tuple(self.upserts))
        if len(self.upserts) > TRAJECTORY_PAGE_RECORD_LIMIT:
            raise TrajectoryValidationError(
                f"delta.upserts exceeds {TRAJECTORY_PAGE_RECORD_LIMIT} values"
            )
        if any(not isinstance(upsert, TrajectoryUpsert) for upsert in self.upserts):
            raise TrajectoryValidationError("delta.upserts must contain TrajectoryUpsert values")
        if self.panel_state is not None and not isinstance(self.panel_state, PanelStateInfo):
            raise TrajectoryValidationError("delta.panel_state must be PanelStateInfo or null")
        if type(self.resync_required) is not bool:
            raise TrajectoryValidationError("delta.resync_required must be a boolean")
        if self.reason is not None and not isinstance(self.reason, str):
            raise TrajectoryValidationError("delta.reason must be a string or null")
        if self.reason is not None:
            object.__setattr__(self, "reason", ContentPreview.from_text(self.reason).text)

    def to_wire(self) -> dict[str, object]:
        return {
            "stream_id": self.stream_id,
            "cursor": self.cursor,
            "upserts": [upsert.to_wire() for upsert in self.upserts],
            "panel_state": self.panel_state.to_wire() if self.panel_state is not None else None,
            "resync_required": self.resync_required,
            "reason": self.reason,
        }

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = mapping(value, "trajectory delta")
        keys(
            data,
            required={"stream_id"},
            optional={"cursor", "upserts", "panel_state", "resync_required", "reason"},
            label="trajectory delta",
        )
        return cls(
            stream_id=string(data["stream_id"], "delta.stream_id"),
            cursor=string_or_none(data.get("cursor"), "delta.cursor"),
            upserts=tuple(
                TrajectoryUpsert.from_wire(item)
                for item in sequence(data.get("upserts", []), "delta.upserts")
            ),
            panel_state=(
                PanelStateInfo.from_wire(data["panel_state"])
                if data.get("panel_state") is not None
                else None
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
