"""Cached, harness-agnostic trajectory analysis."""

from regie.trajectory.rich.analysis.index import build_analysis_index, empty_analysis_index
from regie.trajectory.rich.analysis.models import (
    DelegationActivity,
    FileActivity,
    FileOperationActivity,
    ProblemActivity,
    ResourceActivity,
    ResourceValues,
    TrajectoryAnalysisIndex,
    WaterfallProjection,
    WaterfallRow,
)

__all__ = [
    "DelegationActivity",
    "FileActivity",
    "FileOperationActivity",
    "ProblemActivity",
    "ResourceActivity",
    "ResourceValues",
    "TrajectoryAnalysisIndex",
    "WaterfallProjection",
    "WaterfallRow",
    "build_analysis_index",
    "empty_analysis_index",
]
