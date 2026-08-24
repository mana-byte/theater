"""Explicit wire helpers for callers that do not use value methods directly."""

from __future__ import annotations

from theater.trajectory.page import TrajectoryDelta, TrajectoryPage
from theater.trajectory.records import TrajectoryRecord
from theater.trajectory.tools import TrajectoryToolOperation


def to_wire(
    value: TrajectoryRecord | TrajectoryPage | TrajectoryDelta | TrajectoryToolOperation,
) -> dict[str, object]:
    """Serialize one supported trajectory value through its explicit schema."""
    if not isinstance(
        value,
        (
            TrajectoryRecord,
            TrajectoryPage,
            TrajectoryDelta,
            TrajectoryToolOperation,
        ),
    ):
        raise TypeError("unsupported trajectory wire value")
    return value.to_wire()


def from_wire(
    value_type: (
        type[TrajectoryRecord]
        | type[TrajectoryPage]
        | type[TrajectoryDelta]
        | type[TrajectoryToolOperation]
    ),
    value: object,
) -> TrajectoryRecord | TrajectoryPage | TrajectoryDelta | TrajectoryToolOperation:
    """Deserialize one supported trajectory value with strict validation."""
    if value_type is TrajectoryRecord:
        return TrajectoryRecord.from_wire(value)
    if value_type is TrajectoryPage:
        return TrajectoryPage.from_wire(value)
    if value_type is TrajectoryDelta:
        return TrajectoryDelta.from_wire(value)
    if value_type is TrajectoryToolOperation:
        return TrajectoryToolOperation.from_wire(value)
    raise TypeError("unsupported trajectory wire value type")


__all__ = ["from_wire", "to_wire"]
