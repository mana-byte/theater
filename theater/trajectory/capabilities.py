"""Trajectory harness capability contract."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from theater.trajectory.enums import TrajectoryValidationError
from theater.trajectory.validation import enum_value, keys, mapping, sequence


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


@dataclass(frozen=True, slots=True)
class TrajectoryCapabilities:
    supported: frozenset[TrajectoryFeature] = frozenset()
    unsupported: frozenset[TrajectoryFeature] = frozenset()
    observed: frozenset[TrajectoryFeature] = frozenset()

    def __post_init__(self) -> None:
        for name in ("supported", "unsupported", "observed"):
            object.__setattr__(self, name, _features(getattr(self, name), f"capabilities.{name}"))
        if self.supported & self.unsupported:
            raise TrajectoryValidationError("a feature cannot be both supported and unsupported")

    def support_for(self, feature: TrajectoryFeature) -> TrajectorySupport:
        value = enum_value(TrajectoryFeature, feature, "capabilities.feature")
        if value in self.supported:
            return TrajectorySupport.SUPPORTED
        if value in self.unsupported:
            return TrajectorySupport.UNSUPPORTED
        return TrajectorySupport.UNKNOWN

    def to_wire(self) -> dict[str, object]:
        return {
            "supported": _wire_features(self.supported),
            "unsupported": _wire_features(self.unsupported),
            "observed": _wire_features(self.observed),
        }

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = mapping(value, "trajectory capabilities")
        keys(
            data,
            required=set(),
            optional={"supported", "unsupported", "observed"},
            label="trajectory capabilities",
        )
        return cls(
            supported=_wire_feature_set(data.get("supported", []), "capabilities.supported"),
            unsupported=_wire_feature_set(data.get("unsupported", []), "capabilities.unsupported"),
            observed=_wire_feature_set(data.get("observed", []), "capabilities.observed"),
        )


def _features(value: object, label: str) -> frozenset[TrajectoryFeature]:
    if not isinstance(value, frozenset):
        raise TrajectoryValidationError(f"{label} must be a frozenset of TrajectoryFeature values")
    if any(not isinstance(item, TrajectoryFeature) for item in value):
        raise TrajectoryValidationError(f"{label} must contain TrajectoryFeature values")
    return value


def _wire_feature_set(value: object, label: str) -> frozenset[TrajectoryFeature]:
    items = tuple(
        enum_value(TrajectoryFeature, item, f"{label}[]") for item in sequence(value, label)
    )
    if len(set(items)) != len(items):
        raise TrajectoryValidationError(f"{label} must not repeat a feature")
    return frozenset(items)


def _wire_features(features: frozenset[TrajectoryFeature]) -> list[str]:
    return [feature.value for feature in TrajectoryFeature if feature in features]


__all__ = ["TrajectoryCapabilities", "TrajectoryFeature", "TrajectorySupport"]
