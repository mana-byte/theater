"""Request and nested-tool latency waterfall projection."""

from __future__ import annotations

from collections.abc import Mapping

from theater.regie.trajectory.analysis.models import WaterfallProjection, WaterfallRow
from theater.regie.trajectory.constants import (
    TRAJECTORY_INSIGHT_ROW_LIMIT,
    WATERFALL_MAX_DEPTH,
    WATERFALL_ROWS_PER_REQUEST,
)
from theater.regie.trajectory.request_rows import RequestIndex
from theater.regie.trajectory.tool_rows import ToolIndex
from theater.trajectory import Timing, TrajectoryRequest, TrajectoryToolOperation


def timing_interval(timing: Timing | None) -> tuple[float, float] | None:
    if timing is None or timing.start is None:
        return None
    end = timing.end
    if end is None and timing.duration_ms is not None:
        end = timing.start + timing.duration_ms / 1000
    if end is None or end < timing.start:
        return None
    return timing.start, end


def request_anchor(request: TrajectoryRequest) -> str:
    return (request.model_record_ids or request.record_ids)[0]


def request_label(request: TrajectoryRequest) -> str:
    model = request.model or "model unknown"
    if request.provider and not model.startswith(f"{request.provider}/"):
        return f"{request.provider}/{model}"
    return model


def operation_position(
    operation: TrajectoryToolOperation,
    positions: Mapping[str, int],
) -> int:
    members = (*operation.call_record_ids, *operation.result_record_ids)
    return min((positions.get(record_id, len(positions)) for record_id in members), default=0)


def _tool_depth(
    operation: TrajectoryToolOperation,
    by_call: Mapping[str, TrajectoryToolOperation],
) -> int:
    depth = 0
    parent = operation.parent_call_id
    seen: set[str] = set()
    while parent is not None and parent not in seen and depth < WATERFALL_MAX_DEPTH:
        seen.add(parent)
        ancestor = by_call.get(parent)
        if ancestor is None:
            break
        depth += 1
        parent = ancestor.parent_call_id
    return depth


def build_waterfalls(
    requests: RequestIndex,
    tools: ToolIndex,
    positions: Mapping[str, int],
) -> tuple[WaterfallProjection, ...]:
    result: list[WaterfallProjection] = []
    for request in requests.ordered:
        operation_ids: list[str] = []
        seen_operations: set[str] = set()
        for record_id in request.tool_record_ids:
            operation_id = tools.by_record_id.get(record_id)
            if operation_id is not None and operation_id not in seen_operations:
                seen_operations.add(operation_id)
                operation_ids.append(operation_id)
        operations = [tools.by_id[operation_id] for operation_id in operation_ids]
        operations.sort(key=lambda operation: operation_position(operation, positions))
        anchor = request_anchor(request)
        rows = [
            WaterfallRow(
                key=f"request:{request.request_id}",
                label=request_label(request),
                record_id=anchor,
                member_record_ids=request.record_ids,
                timing=request.timing,
                status=request.status,
                request=True,
            )
        ]
        by_call = {operation.call_id: operation for operation in operations if operation.call_id}
        for operation in operations[-(WATERFALL_ROWS_PER_REQUEST - 1) :]:
            members = (*operation.call_record_ids, *operation.result_record_ids)
            rows.append(
                WaterfallRow(
                    key=operation.operation_id,
                    label=operation.tool_name or "unknown tool",
                    record_id=(operation.call_record_ids or operation.result_record_ids)[0],
                    member_record_ids=members,
                    timing=operation.timing,
                    status=operation.status,
                    depth=1 + _tool_depth(operation, by_call),
                )
            )
        intervals = [interval for row in rows if (interval := timing_interval(row.timing))]
        result.append(
            WaterfallProjection(
                request_id=request.request_id,
                label=request_label(request),
                record_ids=request.record_ids,
                rows=tuple(rows),
                start=min((interval[0] for interval in intervals), default=None),
                end=max((interval[1] for interval in intervals), default=None),
                first_token=request.timing.first_token if request.timing is not None else None,
            )
        )
    return tuple(result[-TRAJECTORY_INSIGHT_ROW_LIMIT:])


__all__ = [
    "build_waterfalls",
    "operation_position",
    "request_anchor",
    "request_label",
    "timing_interval",
]
