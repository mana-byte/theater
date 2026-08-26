"""Cached, local diagnostic projections for the trajectory surface."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import batched
from types import MappingProxyType

from theater.constants.trajectory import TRAJECTORY_MAX_GROUP_RECORD_IDS
from theater.regie.trajectory.analysis import TrajectoryAnalysisIndex, empty_analysis_index
from theater.regie.trajectory.enums import DiagnosticView
from theater.regie.trajectory.ordering import TrajectoryOrdering
from theater.regie.trajectory.request_rows import RequestIndex
from theater.regie.trajectory.tool_rows import ToolIndex
from theater.trajectory import (
    GroupKind,
    TrajectoryGroup,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryStatus,
)
from theater.trajectory.tools import TrajectoryToolIdentity, TrajectoryToolOperation

_ACTIVE = frozenset({TrajectoryStatus.PENDING, TrajectoryStatus.RUNNING, TrajectoryStatus.PARTIAL})
_ERRORS = frozenset(
    {
        TrajectoryStatus.ERROR,
        TrajectoryStatus.INTERRUPTED,
        TrajectoryStatus.TIMEOUT,
        TrajectoryStatus.CANCELLED,
    }
)


@dataclass(frozen=True, slots=True)
class DiagnosticProjection:
    record_ids: frozenset[str]
    ordered_record_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DiagnosticIndex:
    by_view: Mapping[DiagnosticView, DiagnosticProjection]

    def projection_for(self, view: DiagnosticView) -> DiagnosticProjection:
        return self.by_view[view]


def empty_diagnostic_index() -> DiagnosticIndex:
    return DiagnosticIndex(
        MappingProxyType({view: DiagnosticProjection(frozenset()) for view in DiagnosticView})
    )


def _members(index: ToolIndex, operation: TrajectoryToolOperation) -> tuple[str, ...]:
    return index.members_by_id[operation.operation_id]


def _record_duration(record: TrajectoryRecord) -> float | None:
    return record.timing.duration_ms if record.timing is not None else None


def _slow_projection(
    records: Sequence[TrajectoryRecord], request_index: RequestIndex, tool_index: ToolIndex
) -> DiagnosticProjection:
    positions = {record.record_id: position for position, record in enumerate(records)}
    by_id = {record.record_id: record for record in records}
    tool_members = frozenset(tool_index.by_record_id)
    rows: list[tuple[float, int, str, tuple[str, ...]]] = []
    scheduled: set[str] = set()
    for request in request_index.ordered:
        duration = request.timing.duration_ms if request.timing is not None else None
        members = tuple(
            record_id
            for record_id in request.record_ids
            if record_id in by_id
            and by_id[record_id].lane is TrajectoryLane.MODEL
            and record_id not in tool_members
        )
        if duration is None or not members:
            continue
        rows.append(
            (
                duration,
                min(positions[record_id] for record_id in members),
                request.request_id,
                members,
            )
        )
        scheduled.update(members)
    for record in records:
        duration = _record_duration(record)
        if (
            record.record_id in scheduled
            or record.record_id in tool_members
            or record.lane is not TrajectoryLane.MODEL
            or duration is None
        ):
            continue
        rows.append((duration, positions[record.record_id], record.record_id, (record.record_id,)))
    for operation in tool_index.ordered:
        timing = operation.timing
        if timing is None or timing.duration_ms is None:
            continue
        members = _members(tool_index, operation)
        rows.append(
            (
                timing.duration_ms,
                min(positions[record_id] for record_id in members),
                operation.operation_id,
                members,
            )
        )
    ordered = tuple(
        record_id
        for _duration, _position, _identity, members in sorted(
            rows, key=lambda row: (-row[0], row[1], row[2])
        )
        for record_id in members
    )
    return DiagnosticProjection(frozenset(ordered), ordered)


def build_diagnostic_index(
    records: Iterable[TrajectoryRecord],
    request_index: RequestIndex,
    tool_index: ToolIndex,
    analysis_index: TrajectoryAnalysisIndex | None = None,
) -> DiagnosticIndex:
    ordered = tuple(records)
    analysis = analysis_index or empty_analysis_index()
    all_ids = frozenset(record.record_id for record in ordered)
    running = {record.record_id for record in ordered if record.status in _ACTIVE}
    errors = {record.record_id for record in ordered if record.status in _ERRORS}
    tools = set(tool_index.by_record_id)
    for operation in tool_index.ordered:
        members = _members(tool_index, operation)
        if operation.status in _ACTIVE or operation.identity is not TrajectoryToolIdentity.MATCHED:
            running.update(members)
        if operation.status in _ERRORS:
            errors.update(members)
    errors.update(
        record_id for problem in analysis.problems for record_id in problem.member_record_ids
    )
    projections = {
        DiagnosticView.ALL: DiagnosticProjection(all_ids),
        DiagnosticView.RUNNING: DiagnosticProjection(frozenset(running)),
        DiagnosticView.ERRORS: DiagnosticProjection(frozenset(errors)),
        DiagnosticView.SLOW: _slow_projection(ordered, request_index, tool_index),
        DiagnosticView.TOOLS: DiagnosticProjection(frozenset(tools)),
        DiagnosticView.WATERFALL: DiagnosticProjection(
            frozenset(
                record_id for waterfall in analysis.waterfalls for record_id in waterfall.record_ids
            )
        ),
        DiagnosticView.FILES: DiagnosticProjection(
            frozenset(record_id for row in analysis.files for record_id in row.record_ids)
        ),
        DiagnosticView.RESOURCES: DiagnosticProjection(
            frozenset(record_id for row in analysis.resources for record_id in row.record_ids)
        ),
        DiagnosticView.DELEGATION: DiagnosticProjection(
            frozenset(record_id for row in analysis.delegations for record_id in row.record_ids)
        ),
        DiagnosticView.COORDINATION: DiagnosticProjection(
            frozenset(
                record.record_id for record in ordered if record.lane is TrajectoryLane.THEATER
            )
        ),
    }
    return DiagnosticIndex(MappingProxyType(projections))


def ordering_for_projection(
    records: Sequence[TrajectoryRecord], projection: DiagnosticProjection
) -> TrajectoryOrdering | None:
    if not projection.ordered_record_ids:
        return None
    by_id = {record.record_id: record for record in records}
    ordered = tuple(
        by_id[record_id] for record_id in projection.ordered_record_ids if record_id in by_id
    )
    if not ordered:
        return None
    groups = tuple(
        TrajectoryGroup(
            group_id=f"diagnostic:slow:{index}",
            kind=GroupKind.BETWEEN_TURNS,
            label="Slow operations",
            record_ids=record_ids,
        )
        for index, record_ids in enumerate(
            batched(
                (record.record_id for record in ordered),
                TRAJECTORY_MAX_GROUP_RECORD_IDS,
            )
        )
    )
    return TrajectoryOrdering(
        source=ordered,
        groups=groups,
        records=ordered,
        _units=MappingProxyType({id(group): group.record_ids for group in groups}),
    )


__all__ = [
    "DiagnosticIndex",
    "DiagnosticProjection",
    "build_diagnostic_index",
    "empty_diagnostic_index",
    "ordering_for_projection",
]
