"""Standalone régie trajectory components.

The package boundary is deliberately narrow: callers provide decoded-compatible
daemon clients to :class:`TrajectoryController` and mount :class:`TrajectoryView`.
All wire validation, bounded participant state, search, and focus messages stay
inside this package until the app integration wave.
"""

from theater.regie.trajectory.controller import DaemonClientCompatible, TrajectoryController
from theater.regie.trajectory.models import (
    ContentFormat,
    ContentPreview,
    Coverage,
    DetailField,
    FilterDimension,
    FocusRegion,
    InspectorTab,
    Lane,
    OrderMode,
    PanelInfo,
    PanelStatus,
    RecordKind,
    RecordStatus,
    Timing,
    TimingProvenance,
    TrajectoryFollow,
    TrajectoryPage,
    TrajectoryRecord,
    Usage,
    WireDecodeError,
    clip_utf8,
)
from theater.regie.trajectory.search import (
    FilterCounts,
    SearchResult,
    TrajectoryFilters,
    fuzzy_subsequence_score,
    search_records,
)
from theater.regie.trajectory.state import ParticipantTrajectoryState, TrajectoryStateStore
from theater.regie.trajectory.view import (
    ReturnToTree,
    TrajectoryCopyRequested,
    TrajectoryParticipantSelected,
    TrajectoryRetryRequested,
    TrajectoryView,
)

__all__ = [
    "ContentFormat",
    "ContentPreview",
    "Coverage",
    "DaemonClientCompatible",
    "DetailField",
    "FilterCounts",
    "FilterDimension",
    "FocusRegion",
    "InspectorTab",
    "Lane",
    "OrderMode",
    "PanelInfo",
    "PanelStatus",
    "ParticipantTrajectoryState",
    "RecordKind",
    "RecordStatus",
    "ReturnToTree",
    "SearchResult",
    "Timing",
    "TimingProvenance",
    "TrajectoryController",
    "TrajectoryCopyRequested",
    "TrajectoryFilters",
    "TrajectoryFollow",
    "TrajectoryPage",
    "TrajectoryParticipantSelected",
    "TrajectoryRecord",
    "TrajectoryRetryRequested",
    "TrajectoryStateStore",
    "TrajectoryView",
    "Usage",
    "WireDecodeError",
    "clip_utf8",
    "fuzzy_subsequence_score",
    "search_records",
]
