"""Standalone Régie trajectory components and their narrow adapter surface."""

from typing import TYPE_CHECKING

from regie.trajectory.domain import (
    ContentFormat,
    ContentPreview,
    DetailField,
    LinkDirection,
    PanelState,
    PanelStateInfo,
    ParticipantLink,
    Timing,
    TimingProvenance,
    TrajectoryCapabilities,
    TrajectoryCoverage,
    TrajectoryCurrentOperation,
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
)
from regie.trajectory.rich.controller import DaemonClientCompatible, TrajectoryController
from regie.trajectory.rich.enums import (
    DiagnosticView,
    FilterDimension,
    FocusRegion,
    InspectorTab,
    OrderMode,
    TimelineLane,
)
from regie.trajectory.rich.messages import (
    ReturnToTree,
    TrajectoryBackRequested,
    TrajectoryCopyRequested,
    TrajectoryParticipantSelected,
    TrajectoryRetryRequested,
)
from regie.trajectory.rich.models import decode_delta, decode_location, decode_page
from regie.trajectory.rich.navigation import (
    TrajectoryNavigationHistory,
    TrajectoryNavigationTarget,
)
from regie.trajectory.rich.render.pagination import LedgerPage, paginate_search_result
from regie.trajectory.rich.search import (
    FilterCounts,
    SearchResult,
    TrajectoryFilters,
    fuzzy_subsequence_score,
    search_records,
)
from regie.trajectory.rich.state import ParticipantTrajectoryState, TrajectoryStateStore

if TYPE_CHECKING:
    from regie.trajectory.rich.view import TrajectoryView
    from regie.trajectory.rich.widgets.overview import TrajectoryOverviewStrip


def __getattr__(name: str):
    if name == "TrajectoryView":
        from regie.trajectory.rich.view import TrajectoryView

        return TrajectoryView
    if name == "TrajectoryOverviewStrip":
        from regie.trajectory.rich.widgets.overview import TrajectoryOverviewStrip

        return TrajectoryOverviewStrip
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ContentFormat",
    "ContentPreview",
    "DaemonClientCompatible",
    "DetailField",
    "DiagnosticView",
    "FilterCounts",
    "FilterDimension",
    "FocusRegion",
    "InspectorTab",
    "LedgerPage",
    "LinkDirection",
    "OrderMode",
    "PanelState",
    "PanelStateInfo",
    "ParticipantLink",
    "ParticipantTrajectoryState",
    "ReturnToTree",
    "SearchResult",
    "TimelineLane",
    "Timing",
    "TimingProvenance",
    "TrajectoryBackRequested",
    "TrajectoryCapabilities",
    "TrajectoryController",
    "TrajectoryCopyRequested",
    "TrajectoryCoverage",
    "TrajectoryCurrentOperation",
    "TrajectoryDelta",
    "TrajectoryFilters",
    "TrajectoryGroup",
    "TrajectoryKind",
    "TrajectoryLane",
    "TrajectoryNavigationHistory",
    "TrajectoryNavigationTarget",
    "TrajectoryOverview",
    "TrajectoryOverviewStrip",
    "TrajectoryPage",
    "TrajectoryParticipantSelected",
    "TrajectoryRecord",
    "TrajectoryRetryRequested",
    "TrajectoryStateStore",
    "TrajectoryStatus",
    "TrajectoryUpsert",
    "TrajectoryUsage",
    "TrajectoryValidationError",
    "TrajectoryView",
    "decode_delta",
    "decode_location",
    "decode_page",
    "fuzzy_subsequence_score",
    "paginate_search_result",
    "search_records",
]
