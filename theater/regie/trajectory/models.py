"""The sole Régie adapter boundary for canonical trajectory wire values."""

from __future__ import annotations

import json

from theater.regie.trajectory.constants import TRAJECTORY_RESPONSE_MAX_BYTES
from theater.regie.trajectory.enums import FilterDimension, FocusRegion, InspectorTab, OrderMode
from theater.trajectory import (
    ContentFormat,
    ContentPreview,
    CoverageGap,
    DetailField,
    LinkDirection,
    PanelState,
    PanelStateInfo,
    ParticipantLink,
    Timing,
    TimingProvenance,
    TrajectoryCapabilities,
    TrajectoryCoverage,
    TrajectoryDelta,
    TrajectoryGroup,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryOverview,
    TrajectoryPage,
    TrajectoryRecord,
    TrajectoryStatus,
    TrajectoryUpsert,
    TrajectoryUsage,
    TrajectoryValidationError,
    bounded_preview,
    clip_utf8,
)


def decode_page(value: object) -> TrajectoryPage:
    """Decode one canonical snapshot or older-page response."""
    if isinstance(value, TrajectoryPage):
        _reject_oversized(value.to_wire(), "trajectory page")
        return value
    _reject_oversized(value, "trajectory page")
    return TrajectoryPage.from_wire(value)


def decode_delta(value: object) -> TrajectoryDelta:
    """Decode one canonical follow response."""
    if isinstance(value, TrajectoryDelta):
        _reject_oversized(value.to_wire(), "trajectory delta")
        return value
    _reject_oversized(value, "trajectory delta")
    return TrajectoryDelta.from_wire(value)


def _reject_oversized(value: object, label: str) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return
    if len(encoded) > TRAJECTORY_RESPONSE_MAX_BYTES:
        raise TrajectoryValidationError(
            f"{label} exceeds {TRAJECTORY_RESPONSE_MAX_BYTES} encoded bytes"
        )


__all__ = [
    "ContentFormat",
    "ContentPreview",
    "CoverageGap",
    "DetailField",
    "FilterDimension",
    "FocusRegion",
    "InspectorTab",
    "LinkDirection",
    "OrderMode",
    "PanelState",
    "PanelStateInfo",
    "ParticipantLink",
    "Timing",
    "TimingProvenance",
    "TrajectoryCapabilities",
    "TrajectoryCoverage",
    "TrajectoryDelta",
    "TrajectoryGroup",
    "TrajectoryKind",
    "TrajectoryLane",
    "TrajectoryOverview",
    "TrajectoryPage",
    "TrajectoryRecord",
    "TrajectoryStatus",
    "TrajectoryUpsert",
    "TrajectoryUsage",
    "TrajectoryValidationError",
    "bounded_preview",
    "clip_utf8",
    "decode_delta",
    "decode_page",
]
