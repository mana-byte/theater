"""The single public adapter boundary for trajectory wire values and runtime state."""

from theater.regie.trajectory.constants import (
    MAX_DETAIL_BYTES,
    MAX_FIELD_BYTES,
    MAX_INSPECTOR_RATIO,
    MAX_LOADED_BYTES,
    MAX_LOADED_RECORDS,
    MAX_PAGE_RECORDS,
    MAX_RESPONSE_BYTES,
    MIN_INSPECTOR_RATIO,
)
from theater.regie.trajectory.enums import (
    ContentFormat,
    FilterDimension,
    FocusRegion,
    InspectorTab,
    Lane,
    LinkDirection,
    OrderMode,
    PanelStatus,
    RecordKind,
    RecordStatus,
    TimingProvenance,
)
from theater.regie.trajectory.state import ParticipantTrajectoryState, TrajectoryStateStore
from theater.regie.trajectory.wire import (
    ContentPreview,
    Coverage,
    CoverageGap,
    DetailField,
    FollowDelta,
    GroupMetadata,
    PanelInfo,
    ParticipantLink,
    Record,
    Timing,
    TrajectoryFollow,
    TrajectoryPage,
    TrajectoryRecord,
    Usage,
    WireDecodeError,
    clip_utf8,
)


def decode_page(value: object, *, participant_id: str | None = None) -> TrajectoryPage:
    """Decode one bounded snapshot/page response."""
    return TrajectoryPage.from_wire(value, participant_id=participant_id)


def decode_follow(value: object, *, participant_id: str | None = None) -> TrajectoryFollow:
    """Decode one bounded ordinary follow response."""
    return TrajectoryFollow.from_wire(value, participant_id=participant_id)


__all__ = [
    "MAX_DETAIL_BYTES",
    "MAX_FIELD_BYTES",
    "MAX_INSPECTOR_RATIO",
    "MAX_LOADED_BYTES",
    "MAX_LOADED_RECORDS",
    "MAX_PAGE_RECORDS",
    "MAX_RESPONSE_BYTES",
    "MIN_INSPECTOR_RATIO",
    "ContentFormat",
    "ContentPreview",
    "Coverage",
    "CoverageGap",
    "DetailField",
    "FilterDimension",
    "FocusRegion",
    "FollowDelta",
    "GroupMetadata",
    "InspectorTab",
    "Lane",
    "LinkDirection",
    "OrderMode",
    "PanelInfo",
    "PanelStatus",
    "ParticipantLink",
    "ParticipantTrajectoryState",
    "Record",
    "RecordKind",
    "RecordStatus",
    "Timing",
    "TimingProvenance",
    "TrajectoryFollow",
    "TrajectoryPage",
    "TrajectoryRecord",
    "TrajectoryStateStore",
    "Usage",
    "WireDecodeError",
    "clip_utf8",
    "decode_follow",
    "decode_page",
]
