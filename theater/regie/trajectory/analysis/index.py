"""Composition root for cached trajectory analysis."""

from __future__ import annotations

from collections.abc import Iterable
from types import MappingProxyType

from theater.regie.trajectory.analysis.activity import (
    build_delegations,
    build_problems,
    build_resources,
)
from theater.regie.trajectory.analysis.files import build_file_activity
from theater.regie.trajectory.analysis.models import TrajectoryAnalysisIndex
from theater.regie.trajectory.analysis.waterfall import build_waterfalls
from theater.regie.trajectory.render.requests import RequestIndex
from theater.regie.trajectory.render.tools import ToolIndex
from theater.trajectory import TrajectoryRecord


def empty_analysis_index() -> TrajectoryAnalysisIndex:
    return TrajectoryAnalysisIndex()


def build_analysis_index(
    records: Iterable[TrajectoryRecord],
    request_index: RequestIndex,
    tool_index: ToolIndex,
) -> TrajectoryAnalysisIndex:
    ordered = tuple(records)
    positions = {record.record_id: index for index, record in enumerate(ordered)}
    waterfalls = build_waterfalls(request_index, tool_index, positions)
    waterfall_by_id = {waterfall.request_id: waterfall for waterfall in waterfalls}
    waterfall_id_by_record = {
        record_id: waterfall.request_id
        for waterfall in waterfalls
        for record_id in waterfall.record_ids
    }
    return TrajectoryAnalysisIndex(
        waterfalls=waterfalls,
        waterfall_by_id=MappingProxyType(waterfall_by_id),
        waterfall_id_by_record=MappingProxyType(waterfall_id_by_record),
        files=build_file_activity(tool_index, positions),
        delegations=build_delegations(ordered),
        resources=build_resources(request_index),
        problems=build_problems(ordered, tool_index, positions),
    )


__all__ = ["build_analysis_index", "empty_analysis_index"]
