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
    TRAJECTORY_OVERVIEW_MAX_DURATION_MS,
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
class TrajectorySlowOperation:
    record_id: str
    operation_id: str
    label: str
    duration_ms: float
    status: TrajectoryStatus
    model: str | None = None
    tool_name: str | None = None

    def __post_init__(self) -> None:
        for name in ("record_id", "operation_id", "label"):
            object.__setattr__(
                self,
                name,
                bounded_text(
                    getattr(self, name),
                    max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                    label=f"overview.slowest.{name}",
                    nonempty=True,
                ),
            )
        object.__setattr__(
            self, "status", enum_value(TrajectoryStatus, self.status, "overview.slowest.status")
        )
        for name in ("model", "tool_name"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    bounded_text(
                        value,
                        max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                        label=f"overview.slowest.{name}",
                        nonempty=True,
                    ),
                )
        if (
            type(self.duration_ms) not in (int, float)
            or not math.isfinite(self.duration_ms)
            or self.duration_ms < 0
        ):
            raise TrajectoryValidationError("overview.slowest.duration_ms must be non-negative")

    def to_wire(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "operation_id": self.operation_id,
            "label": self.label,
            "duration_ms": self.duration_ms,
            "status": self.status.value,
            "model": self.model,
            "tool_name": self.tool_name,
        }

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = mapping(value, "trajectory slow operation")
        keys(
            data,
            required={"record_id", "operation_id", "label", "duration_ms", "status"},
            optional={"model", "tool_name"},
            label="trajectory slow operation",
        )
        duration_ms = number_or_none(data["duration_ms"], "overview.slowest.duration_ms")
        if duration_ms is None:
            raise TrajectoryValidationError("overview.slowest.duration_ms must be a number")
        return cls(
            record_id=string(data["record_id"], "overview.slowest.record_id"),
            operation_id=string(data["operation_id"], "overview.slowest.operation_id"),
            label=string(data["label"], "overview.slowest.label"),
            duration_ms=duration_ms,
            status=enum_value(TrajectoryStatus, data["status"], "overview.slowest.status"),
            model=string_or_none(data.get("model"), "overview.slowest.model"),
            tool_name=string_or_none(data.get("tool_name"), "overview.slowest.tool_name"),
        )


@dataclass(frozen=True, slots=True)
class TrajectoryErrorDiagnostics:
    error_count: int = 0
    retry_count: int | None = None

    def __post_init__(self) -> None:
        if (
            type(self.error_count) is not int
            or not 0 <= self.error_count <= TRAJECTORY_OVERVIEW_MAX_COUNT
        ):
            raise TrajectoryValidationError(
                "overview.diagnostics.error_count must be a bounded integer"
            )
        if self.retry_count is not None and (
            type(self.retry_count) is not int
            or not 0 <= self.retry_count <= TRAJECTORY_OVERVIEW_MAX_COUNT
        ):
            raise TrajectoryValidationError(
                "overview.diagnostics.retry_count must be a bounded integer or null"
            )

    def to_wire(self) -> dict[str, object]:
        return {"error_count": self.error_count, "retry_count": self.retry_count}

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = mapping(value, "trajectory error diagnostics")
        keys(
            data,
            required=set(),
            optional={"error_count", "retry_count"},
            label="trajectory error diagnostics",
        )
        return cls(
            error_count=integer(data.get("error_count", 0), "overview.diagnostics.error_count"),
            retry_count=(
                integer(data["retry_count"], "overview.diagnostics.retry_count")
                if data.get("retry_count") is not None
                else None
            ),
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
    estimated_cost_usd: float | None = None
    unknown_cost_usd: float | None = None
    active_duration_ms: float | None = None
    totals_saturated: bool = False
    current: TrajectoryCurrentOperation | None = None
    latest_problem: TrajectoryProblem | None = None
    slowest_model_operation: TrajectorySlowOperation | None = None
    slowest_tool_operation: TrajectorySlowOperation | None = None
    diagnostics: TrajectoryErrorDiagnostics | None = None

    def __post_init__(self) -> None:  # noqa: PLR0912
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
        for name in ("reported_cost_usd", "estimated_cost_usd", "unknown_cost_usd"):
            value = getattr(self, name)
            if value is not None and (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0 <= value <= TRAJECTORY_OVERVIEW_MAX_COST_USD
            ):
                raise TrajectoryValidationError(
                    f"overview.{name} must be a bounded non-negative number or null"
                )
        if self.active_duration_ms is not None and (
            type(self.active_duration_ms) not in (int, float)
            or not math.isfinite(self.active_duration_ms)
            or not 0 <= self.active_duration_ms <= TRAJECTORY_OVERVIEW_MAX_DURATION_MS
        ):
            raise TrajectoryValidationError(
                "overview.active_duration_ms must be a bounded non-negative number or null"
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
        for name in ("slowest_model_operation", "slowest_tool_operation"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, TrajectorySlowOperation):
                raise TrajectoryValidationError(
                    f"overview.{name} must be TrajectorySlowOperation or null"
                )
        if self.diagnostics is not None and not isinstance(
            self.diagnostics, TrajectoryErrorDiagnostics
        ):
            raise TrajectoryValidationError(
                "overview.diagnostics must be TrajectoryErrorDiagnostics or null"
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
        value: dict[str, object] = {
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
        if self.active_duration_ms is not None:
            value["active_duration_ms"] = self.active_duration_ms
        if self.estimated_cost_usd is not None:
            value["estimated_cost_usd"] = self.estimated_cost_usd
        if self.unknown_cost_usd is not None:
            value["unknown_cost_usd"] = self.unknown_cost_usd
        if self.slowest_model_operation is not None:
            value["slowest_model_operation"] = self.slowest_model_operation.to_wire()
        if self.slowest_tool_operation is not None:
            value["slowest_tool_operation"] = self.slowest_tool_operation.to_wire()
        if self.diagnostics is not None:
            value["diagnostics"] = self.diagnostics.to_wire()
        return value

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
                "estimated_cost_usd",
                "unknown_cost_usd",
                "active_duration_ms",
                "totals_saturated",
                "current",
                "latest_problem",
                "slowest_model_operation",
                "slowest_tool_operation",
                "diagnostics",
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
            estimated_cost_usd=number_or_none(
                data.get("estimated_cost_usd"), "overview.estimated_cost_usd"
            ),
            unknown_cost_usd=number_or_none(
                data.get("unknown_cost_usd"), "overview.unknown_cost_usd"
            ),
            active_duration_ms=number_or_none(
                data.get("active_duration_ms"), "overview.active_duration_ms"
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
            slowest_model_operation=(
                TrajectorySlowOperation.from_wire(data["slowest_model_operation"])
                if data.get("slowest_model_operation") is not None
                else None
            ),
            slowest_tool_operation=(
                TrajectorySlowOperation.from_wire(data["slowest_tool_operation"])
                if data.get("slowest_tool_operation") is not None
                else None
            ),
            diagnostics=(
                TrajectoryErrorDiagnostics.from_wire(data["diagnostics"])
                if data.get("diagnostics") is not None
                else None
            ),
        )


__all__ = [
    "TrajectoryCurrentOperation",
    "TrajectoryErrorDiagnostics",
    "TrajectoryIncompleteReason",
    "TrajectoryOverview",
    "TrajectoryProblem",
    "TrajectorySlowOperation",
]
