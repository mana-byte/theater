"""Bounded capability and current-scope trajectory values."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from theater.constants.trajectory import (
    TRAJECTORY_IDENTIFIER_MAX_BYTES,
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

_MAX_COUNT = (1 << 31) - 1
_MAX_TOTAL = (1 << 63) - 1


class TrajectoryFeature(StrEnum):
    REQUESTS = "requests"
    MODELS = "models"
    TOOLS = "tools"
    USAGE = "usage"
    TIMING = "timing"
    REASONING = "reasoning"
    CONTEXT = "context"
    RETRIES = "retries"
    LIVE_UPDATES = "live_updates"


class TrajectorySupport(StrEnum):
    UNKNOWN = "unknown"
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"


class TrajectoryScope(StrEnum):
    LOADED = "loaded"


@dataclass(frozen=True, slots=True)
class TrajectoryCapability:
    feature: TrajectoryFeature
    declared: TrajectorySupport = TrajectorySupport.UNKNOWN
    observed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "feature", enum_value(TrajectoryFeature, self.feature, "capability.feature")
        )
        object.__setattr__(
            self, "declared", enum_value(TrajectorySupport, self.declared, "capability.declared")
        )
        if type(self.observed) is not bool:
            raise TrajectoryValidationError("capability.observed must be a boolean")

    def to_wire(self) -> dict[str, object]:
        return {
            "feature": self.feature.value,
            "declared": self.declared.value,
            "observed": self.observed,
        }

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = mapping(value, "trajectory capability")
        keys(
            data,
            required={"feature"},
            optional={"declared", "observed"},
            label="trajectory capability",
        )
        return cls(
            feature=enum_value(TrajectoryFeature, data["feature"], "capability.feature"),
            declared=enum_value(
                TrajectorySupport,
                data.get("declared", TrajectorySupport.UNKNOWN.value),
                "capability.declared",
            ),
            observed=boolean(data.get("observed", False), "capability.observed"),
        )


@dataclass(frozen=True, slots=True)
class TrajectoryCapabilities:
    features: tuple[TrajectoryCapability, ...] = ()

    def __post_init__(self) -> None:
        supplied = tuple(self.features)
        if any(not isinstance(value, TrajectoryCapability) for value in supplied):
            raise TrajectoryValidationError(
                "capabilities.features must contain TrajectoryCapability values"
            )
        by_feature: dict[TrajectoryFeature, TrajectoryCapability] = {}
        for value in supplied:
            if value.feature in by_feature:
                raise TrajectoryValidationError("capabilities.features must not repeat a feature")
            by_feature[value.feature] = value
        object.__setattr__(
            self,
            "features",
            tuple(
                by_feature.get(feature, TrajectoryCapability(feature))
                for feature in TrajectoryFeature
            ),
        )

    @classmethod
    def declared(
        cls,
        *,
        supported: frozenset[TrajectoryFeature] = frozenset(),
        unsupported: frozenset[TrajectoryFeature] = frozenset(),
    ) -> Self:
        overlap = supported & unsupported
        if overlap:
            raise TrajectoryValidationError("a feature cannot be both supported and unsupported")
        return cls(
            tuple(
                TrajectoryCapability(
                    feature,
                    (
                        TrajectorySupport.SUPPORTED
                        if feature in supported
                        else TrajectorySupport.UNSUPPORTED
                        if feature in unsupported
                        else TrajectorySupport.UNKNOWN
                    ),
                )
                for feature in TrajectoryFeature
            )
        )

    def with_observed(self, observed: frozenset[TrajectoryFeature]) -> TrajectoryCapabilities:
        return TrajectoryCapabilities(
            tuple(
                TrajectoryCapability(value.feature, value.declared, value.feature in observed)
                for value in self.features
            )
        )

    def to_wire(self) -> dict[str, object]:
        return {"features": [value.to_wire() for value in self.features]}

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = mapping(value, "trajectory capabilities")
        keys(data, required=set(), optional={"features"}, label="trajectory capabilities")
        return cls(
            tuple(
                TrajectoryCapability.from_wire(item)
                for item in sequence(data.get("features", []), "capabilities.features")
            )
        )


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
class TrajectoryLatestError:
    record_id: str
    summary: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "record_id",
            bounded_text(
                self.record_id,
                max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                label="overview.error.record_id",
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
        data = mapping(value, "trajectory latest error")
        keys(data, required={"record_id"}, optional={"summary"}, label="trajectory latest error")
        return cls(
            record_id=string(data["record_id"], "overview.error.record_id"),
            summary=string(data.get("summary", ""), "overview.error.summary"),
        )


@dataclass(frozen=True, slots=True)
class TrajectoryOverview:
    scope: TrajectoryScope = TrajectoryScope.LOADED
    scope_complete: bool = False
    has_older: bool = False
    has_coverage_gaps: bool = False
    record_count: int = 0
    model_operations: int = 0
    tool_operations: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    reported_cost_usd: float | None = None
    current: TrajectoryCurrentOperation | None = None
    latest_error: TrajectoryLatestError | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", enum_value(TrajectoryScope, self.scope, "overview.scope"))
        for name in ("scope_complete", "has_older", "has_coverage_gaps"):
            if type(getattr(self, name)) is not bool:
                raise TrajectoryValidationError(f"overview.{name} must be a boolean")
        for name in ("record_count", "model_operations", "tool_operations"):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= _MAX_COUNT:
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
            if type(value) is not int or not 0 <= value <= _MAX_TOTAL:
                raise TrajectoryValidationError(
                    f"overview.{name} must be a bounded non-negative integer"
                )
        if self.reported_cost_usd is not None and (
            type(self.reported_cost_usd) not in (int, float)
            or not math.isfinite(self.reported_cost_usd)
            or self.reported_cost_usd < 0
        ):
            raise TrajectoryValidationError(
                "overview.reported_cost_usd must be non-negative or null"
            )
        if self.current is not None and not isinstance(self.current, TrajectoryCurrentOperation):
            raise TrajectoryValidationError(
                "overview.current must be TrajectoryCurrentOperation or null"
            )
        if self.latest_error is not None and not isinstance(
            self.latest_error, TrajectoryLatestError
        ):
            raise TrajectoryValidationError(
                "overview.latest_error must be TrajectoryLatestError or null"
            )

    def to_wire(self) -> dict[str, object]:
        return {
            "scope": self.scope.value,
            "scope_complete": self.scope_complete,
            "has_older": self.has_older,
            "has_coverage_gaps": self.has_coverage_gaps,
            "record_count": self.record_count,
            "model_operations": self.model_operations,
            "tool_operations": self.tool_operations,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "reported_cost_usd": self.reported_cost_usd,
            "current": self.current.to_wire() if self.current is not None else None,
            "latest_error": self.latest_error.to_wire() if self.latest_error is not None else None,
        }

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = mapping(value, "trajectory overview")
        optional = {
            "scope",
            "scope_complete",
            "has_older",
            "has_coverage_gaps",
            "record_count",
            "model_operations",
            "tool_operations",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
            "reported_cost_usd",
            "current",
            "latest_error",
        }
        keys(data, required=set(), optional=optional, label="trajectory overview")
        return cls(
            scope=enum_value(
                TrajectoryScope, data.get("scope", TrajectoryScope.LOADED.value), "overview.scope"
            ),
            scope_complete=boolean(data.get("scope_complete", False), "overview.scope_complete"),
            has_older=boolean(data.get("has_older", False), "overview.has_older"),
            has_coverage_gaps=boolean(
                data.get("has_coverage_gaps", False), "overview.has_coverage_gaps"
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
            current=(
                TrajectoryCurrentOperation.from_wire(data["current"])
                if data.get("current") is not None
                else None
            ),
            latest_error=(
                TrajectoryLatestError.from_wire(data["latest_error"])
                if data.get("latest_error") is not None
                else None
            ),
        )


__all__ = [
    "TrajectoryCapabilities",
    "TrajectoryCapability",
    "TrajectoryCurrentOperation",
    "TrajectoryFeature",
    "TrajectoryLatestError",
    "TrajectoryOverview",
    "TrajectoryScope",
    "TrajectorySupport",
]
