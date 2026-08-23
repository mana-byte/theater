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

Lane = TrajectoryLane
Kind = TrajectoryKind
Status = TrajectoryStatus
Format = ContentFormat
Usage = TrajectoryUsage
RecordKind = TrajectoryKind
RecordStatus = TrajectoryStatus
TrajectoryDetailField = DetailField
TrajectoryContentFormat = ContentFormat
TrajectoryDetail = DetailField
TrajectoryPanelState = PanelState
TrajectoryPanelStateInfo = PanelStateInfo
TrajectoryRecordKind = TrajectoryKind
TrajectoryRecordStatus = TrajectoryStatus
TrajectoryTiming = Timing
TrajectoryTimingProvenance = TimingProvenance

__all__ = [
    "ContentFormat",
    "ContentPreview",
    "CoverageGap",
    "DetailField",
    "Format",
    "GroupKind",
    "Kind",
    "Lane",
    "LinkDirection",
    "PanelState",
    "PanelStateInfo",
    "ParticipantLink",
    "RecordKind",
    "RecordStatus",
    "Status",
    "Timing",
    "TimingProvenance",
    "TrajectoryContentFormat",
    "TrajectoryCoverage",
    "TrajectoryDelta",
    "TrajectoryDetail",
    "TrajectoryDetailField",
    "TrajectoryGroup",
    "TrajectoryKind",
    "TrajectoryLane",
    "TrajectoryPage",
    "TrajectoryPanelState",
    "TrajectoryPanelStateInfo",
    "TrajectoryRecord",
    "TrajectoryRecordKind",
    "TrajectoryRecordStatus",
    "TrajectoryStatus",
    "TrajectoryTiming",
    "TrajectoryTimingProvenance",
    "TrajectoryUpsert",
    "TrajectoryUsage",
    "TrajectoryValidationError",
    "Usage",
    "bound_detail_fields",
    "escape_rich_text",
    "sanitize_text",
]
