"""Compatibility facade for the split trajectory model modules."""

from __future__ import annotations

from theater.trajectory.content import (
    ContentPreview,
    DetailField,
    bound_detail_fields,
    escape_rich_text,
    sanitize_text,
)
from theater.trajectory.enums import (
    ContentFormat,
    GroupKind,
    LinkDirection,
    PanelState,
    TimingProvenance,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryParticipantState,
    TrajectoryStatus,
    TrajectoryValidationError,
)
from theater.trajectory.page import (
    CoverageGap,
    PanelStateInfo,
    TrajectoryCoverage,
    TrajectoryDelta,
    TrajectoryGroup,
    TrajectoryPage,
    TrajectoryUpsert,
)
from theater.trajectory.records import (
    ParticipantLink,
    Timing,
    TrajectoryRecord,
    TrajectoryUsage,
)
from theater.trajectory.usefulness import (
    TrajectoryCapabilities,
    TrajectoryCapability,
    TrajectoryCurrentOperation,
    TrajectoryFeature,
    TrajectoryLatestError,
    TrajectoryOverview,
    TrajectoryScope,
    TrajectorySupport,
)

__all__ = [
    "ContentFormat",
    "ContentPreview",
    "CoverageGap",
    "DetailField",
    "GroupKind",
    "LinkDirection",
    "PanelState",
    "PanelStateInfo",
    "ParticipantLink",
    "Timing",
    "TimingProvenance",
    "TrajectoryCapabilities",
    "TrajectoryCapability",
    "TrajectoryCoverage",
    "TrajectoryCurrentOperation",
    "TrajectoryDelta",
    "TrajectoryFeature",
    "TrajectoryGroup",
    "TrajectoryKind",
    "TrajectoryLane",
    "TrajectoryLatestError",
    "TrajectoryOverview",
    "TrajectoryPage",
    "TrajectoryParticipantState",
    "TrajectoryRecord",
    "TrajectoryScope",
    "TrajectoryStatus",
    "TrajectorySupport",
    "TrajectoryUpsert",
    "TrajectoryUsage",
    "TrajectoryValidationError",
    "bound_detail_fields",
    "escape_rich_text",
    "sanitize_text",
]
