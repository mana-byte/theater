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
    fuzzy_subsequence_score,
)
from regie.trajectory.rich.controller import DaemonClientCompatible, TrajectoryController
from regie.trajectory.rich.enums import (
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
from regie.trajectory.rich.state import ParticipantTrajectoryState, TrajectoryStateStore

if TYPE_CHECKING:
    from regie.trajectory.rich.view import TrajectoryView


def __getattr__(name: str):
    if name == "TrajectoryView":
        from regie.trajectory.rich.view import TrajectoryView

        return TrajectoryView
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ContentFormat",
    "ContentPreview",
    "DaemonClientCompatible",
    "DetailField",
    "FocusRegion",
    "InspectorTab",
    "LinkDirection",
    "OrderMode",
    "PanelState",
    "PanelStateInfo",
    "ParticipantLink",
    "ParticipantTrajectoryState",
    "ReturnToTree",
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
    "TrajectoryGroup",
    "TrajectoryKind",
    "TrajectoryLane",
    "TrajectoryNavigationHistory",
    "TrajectoryNavigationTarget",
    "TrajectoryOverview",
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
]
