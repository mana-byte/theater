"""Bounded current-scope trajectory values."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from theater.constants.trajectory import (
    TRAJECTORY_IDENTIFIER_MAX_BYTES,
    TRAJECTORY_OVERVIEW_MAX_COST_USD,
    TRAJECTORY_OVERVIEW_MAX_COUNT,
    TRAJECTORY_OVERVIEW_MAX_TOKENS,
    TRAJECTORY_OVERVIEW_SUMMARY_MAX_BYTES,
)
from theater.trajectory.content import ContentPreview, bounded_text
from theater.trajectory.enums import (
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryStatus,
    TrajectoryValidationError,
)
from theater.trajectory.validation import (
    boolean,
    enum_value,
    integer,
    keys,
    mapping,
    number_or_none,
    sequence,
    string,
    string_or_none,
)


class TrajectoryIncompleteReason(StrEnum):
    UNKNOWN = "unknown"
    OLDER_HISTORY = "older_history"
    COVERAGE_GAPS = "coverage_gaps"
    CACHE_EVICTED = "cache_evicted"


@dataclass(frozen=True, slots=True)
class TrajectoryCurrentOperation:
    record_id: str
    kind: TrajectoryKind
    lane: TrajectoryLane
    status: TrajectoryStatus
    summary: str = ""
    model: str | None = None
    start: float | None = None
    duration_ms: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "record_id",
            bounded_text(
                self.record_id,
                max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                label="overview.current.record_id",
                nonempty=True,
            ),
        )
        object.__setattr__(
            self, "kind", enum_value(TrajectoryKind, self.kind, "overview.current.kind")
        )
        object.__setattr__(
            self, "lane", enum_value(TrajectoryLane, self.lane, "overview.current.lane")
        )
        object.__setattr__(
            self, "status", enum_value(TrajectoryStatus, self.status, "overview.current.status")
        )
        object.__setattr__(
            self,
            "summary",
            ContentPreview.from_text(
                self.summary, max_bytes=TRAJECTORY_OVERVIEW_SUMMARY_MAX_BYTES
            ).text,
        )
        if self.model is not None:
            object.__setattr__(
                self,
                "model",
                bounded_text(
                    self.model,
                    max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                    label="overview.current.model",
                    nonempty=True,
                ),
            )
        for name in ("start", "duration_ms"):
            value = getattr(self, name)
            if value is not None and (
                type(value) not in (int, float) or not math.isfinite(value) or value < 0
            ):
                raise TrajectoryValidationError(
                    f"overview.current.{name} must be non-negative or null"
                )

    def to_wire(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "kind": self.kind.value,
            "lane": self.lane.value,
            "status": self.status.value,
            "summary": self.summary,
            "model": self.model,
            "start": self.start,
            "duration_ms": self.duration_ms,
        }

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = mapping(value, "trajectory current operation")
        keys(
            data,
            required={"record_id", "kind", "lane", "status"},
            optional={"summary", "model", "start", "duration_ms"},
            label="trajectory current operation",
        )
        return cls(
            record_id=string(data["record_id"], "overview.current.record_id"),
            kind=enum_value(TrajectoryKind, data["kind"], "overview.current.kind"),
            lane=enum_value(TrajectoryLane, data["lane"], "overview.current.lane"),
            status=enum_value(TrajectoryStatus, data["status"], "overview.current.status"),
            summary=string(data.get("summary", ""), "overview.current.summary"),
            model=string_or_none(data.get("model"), "overview.current.model"),
            start=number_or_none(data.get("start"), "overview.current.start"),
            duration_ms=number_or_none(data.get("duration_ms"), "overview.current.duration_ms"),
        )


@dataclass(frozen=True, slots=True)
class TrajectoryProblem:
    record_id: str
    summary: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "record_id",
            bounded_text(
                self.record_id,
                max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                label="overview.problem.record_id",
                nonempty=True,
            ),
        )
        object.__setattr__(
            self,
            "summary",
            ContentPreview.from_text(
                self.summary, max_bytes=TRAJECTORY_OVERVIEW_SUMMARY_MAX_BYTES
            ).text,
        )

    def to_wire(self) -> dict[str, object]:
        return {"record_id": self.record_id, "summary": self.summary}

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = mapping(value, "trajectory problem")
        keys(data, required={"record_id"}, optional={"summary"}, label="trajectory problem")
        return cls(
            record_id=string(data["record_id"], "overview.problem.record_id"),
            summary=string(data.get("summary", ""), "overview.problem.summary"),
        )


@dataclass(frozen=True, slots=True)
class TrajectoryOverview:
    incomplete_reasons: tuple[TrajectoryIncompleteReason, ...] = (
        TrajectoryIncompleteReason.UNKNOWN,
    )
    record_count: int = 0
    model_operations: int = 0
    tool_operations: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    reported_cost_usd: float | None = None
    totals_saturated: bool = False
    current: TrajectoryCurrentOperation | None = None
    latest_problem: TrajectoryProblem | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "incomplete_reasons",
            tuple(
                enum_value(TrajectoryIncompleteReason, value, "overview.incomplete_reasons[]")
                for value in self.incomplete_reasons
            ),
        )
        if len(self.incomplete_reasons) > len(TrajectoryIncompleteReason):
            raise TrajectoryValidationError("overview.incomplete_reasons has too many values")
        if len(set(self.incomplete_reasons)) != len(self.incomplete_reasons):
            raise TrajectoryValidationError("overview.incomplete_reasons must not repeat a value")
        for name in ("record_count", "model_operations", "tool_operations"):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= TRAJECTORY_OVERVIEW_MAX_COUNT:
                raise TrajectoryValidationError(
                    f"overview.{name} must be a bounded non-negative integer"
                )
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
        ):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= TRAJECTORY_OVERVIEW_MAX_TOKENS:
                raise TrajectoryValidationError(
                    f"overview.{name} must be a bounded non-negative integer"
                )
        if self.reported_cost_usd is not None and (
            type(self.reported_cost_usd) not in (int, float)
            or not math.isfinite(self.reported_cost_usd)
            or not 0 <= self.reported_cost_usd <= TRAJECTORY_OVERVIEW_MAX_COST_USD
        ):
            raise TrajectoryValidationError(
                "overview.reported_cost_usd must be a bounded non-negative number or null"
            )
        if type(self.totals_saturated) is not bool:
            raise TrajectoryValidationError("overview.totals_saturated must be a boolean")
        if self.current is not None and not isinstance(self.current, TrajectoryCurrentOperation):
            raise TrajectoryValidationError(
                "overview.current must be TrajectoryCurrentOperation or null"
            )
        if self.latest_problem is not None and not isinstance(
            self.latest_problem, TrajectoryProblem
        ):
            raise TrajectoryValidationError(
                "overview.latest_problem must be TrajectoryProblem or null"
            )

    @property
    def scope_complete(self) -> bool:
        return not self.incomplete_reasons

    @property
    def has_older(self) -> bool:
        return TrajectoryIncompleteReason.OLDER_HISTORY in self.incomplete_reasons

    @property
    def has_coverage_gaps(self) -> bool:
        return TrajectoryIncompleteReason.COVERAGE_GAPS in self.incomplete_reasons

    @property
    def cache_evicted(self) -> bool:
        return TrajectoryIncompleteReason.CACHE_EVICTED in self.incomplete_reasons

    def to_wire(self) -> dict[str, object]:
        return {
            "incomplete_reasons": [value.value for value in self.incomplete_reasons],
            "record_count": self.record_count,
            "model_operations": self.model_operations,
            "tool_operations": self.tool_operations,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "reported_cost_usd": self.reported_cost_usd,
            "totals_saturated": self.totals_saturated,
            "current": self.current.to_wire() if self.current is not None else None,
            "latest_problem": self.latest_problem.to_wire()
            if self.latest_problem is not None
            else None,
        }

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = mapping(value, "trajectory overview")
        keys(
            data,
            required=set(),
            optional={
                "incomplete_reasons",
                "record_count",
                "model_operations",
                "tool_operations",
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
                "reported_cost_usd",
                "totals_saturated",
                "current",
                "latest_problem",
            },
            label="trajectory overview",
        )
        return cls(
            incomplete_reasons=tuple(
                enum_value(TrajectoryIncompleteReason, item, "overview.incomplete_reasons[]")
                for item in sequence(
                    data.get(
                        "incomplete_reasons",
                        [TrajectoryIncompleteReason.UNKNOWN.value],
                    ),
                    "overview.incomplete_reasons",
                )
            ),
            record_count=integer(data.get("record_count", 0), "overview.record_count"),
            model_operations=integer(data.get("model_operations", 0), "overview.model_operations"),
            tool_operations=integer(data.get("tool_operations", 0), "overview.tool_operations"),
            input_tokens=integer(data.get("input_tokens", 0), "overview.input_tokens"),
            output_tokens=integer(data.get("output_tokens", 0), "overview.output_tokens"),
            cache_read_tokens=integer(
                data.get("cache_read_tokens", 0), "overview.cache_read_tokens"
            ),
            cache_write_tokens=integer(
                data.get("cache_write_tokens", 0), "overview.cache_write_tokens"
            ),
            reasoning_tokens=integer(data.get("reasoning_tokens", 0), "overview.reasoning_tokens"),
            reported_cost_usd=number_or_none(
                data.get("reported_cost_usd"), "overview.reported_cost_usd"
            ),
            totals_saturated=boolean(
                data.get("totals_saturated", False), "overview.totals_saturated"
            ),
            current=(
                TrajectoryCurrentOperation.from_wire(data["current"])
                if data.get("current") is not None
                else None
            ),
            latest_problem=(
                TrajectoryProblem.from_wire(data["latest_problem"])
                if data.get("latest_problem") is not None
                else None
            ),
        )


__all__ = [
    "TrajectoryCurrentOperation",
    "TrajectoryIncompleteReason",
    "TrajectoryOverview",
    "TrajectoryProblem",
]
