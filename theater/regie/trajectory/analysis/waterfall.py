"""Turn-scoped model and nested-tool latency waterfall projection."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from hashlib import sha256

from theater.constants.regie_trajectory import (
    TRAJECTORY_INSIGHT_ROW_LIMIT,
    WATERFALL_MAX_DEPTH,
    WATERFALL_ROWS_PER_SCOPE,
)
from theater.regie.trajectory.analysis.models import WaterfallProjection, WaterfallRow
from theater.regie.trajectory.render.requests import RequestIndex
from theater.regie.trajectory.render.tools import ToolIndex
from theater.trajectory import (
    Timing,
    TimingProvenance,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryRequest,
    TrajectoryStatus,
    TrajectoryToolOperation,
)

_ACTIVE_STATUSES = frozenset(
    {TrajectoryStatus.PENDING, TrajectoryStatus.RUNNING, TrajectoryStatus.PARTIAL}
)
_FAILED_STATUSES = frozenset(
    {
        TrajectoryStatus.ERROR,
        TrajectoryStatus.INTERRUPTED,
        TrajectoryStatus.TIMEOUT,
        TrajectoryStatus.CANCELLED,
    }
)


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


def _scope_key(request: TrajectoryRequest) -> tuple[str, str, str, str]:
    kind = "turn" if request.turn_id is not None else "request"
    identity = request.turn_id or request.request_id
    return request.participant_id, request.source_epoch, kind, identity


def _scope_id(key: tuple[str, str, str, str]) -> str:
    kind = key[2]
    digest = sha256("\0".join(key).encode()).hexdigest()
    return f"{kind}:{digest}"


def _request_scope_label(requests: tuple[TrajectoryRequest, ...]) -> str:
    labels = tuple(dict.fromkeys(request_label(request) for request in requests))
    if len(requests) == 1:
        return labels[0]
    model = labels[0] if len(labels) == 1 else "mixed models"
    return f"{model} · {len(requests)} calls"


def _record_scope_label(records: tuple[TrajectoryRecord, ...]) -> str:
    models = tuple(
        dict.fromkeys(
            record.usage.model
            for record in records
            if record.usage is not None and record.usage.model is not None
        )
    )
    if len(models) == 1:
        return models[0]
    return "mixed models" if models else "model activity"


def _scope_label(
    requests: tuple[TrajectoryRequest, ...], records: tuple[TrajectoryRecord, ...]
) -> str:
    return _request_scope_label(requests) if requests else _record_scope_label(records)


def _scope_timing(requests: tuple[TrajectoryRequest, ...]) -> Timing | None:
    timings = tuple(
        timing
        for request in requests
        if (timing := request.timing) is not None and timing_interval(timing) is not None
    )
    if not timings:
        return None
    if len(timings) == 1:
        return timings[0] if len(requests) == 1 else None
    if len(timings) != len(requests):
        return None
    starts = [timing.start for timing in timings if timing.start is not None]
    ends = [timing.end for timing in timings if timing.end is not None]
    if not starts or not ends:
        return None
    start = min(starts)
    end = max(ends)
    first_tokens = [
        timing.first_token
        for timing in timings
        if timing.first_token is not None and start <= timing.first_token <= end
    ]
    return Timing(
        start=start,
        end=end,
        duration_ms=(end - start) * 1_000,
        provenance=TimingProvenance.DERIVED,
        first_token=min(first_tokens) if first_tokens else None,
    )


def _record_scope_timing(
    records: tuple[TrajectoryRecord, ...],
    operations: tuple[TrajectoryToolOperation, ...],
) -> Timing | None:
    """Derive a turn-scope interval from record and tool-operation timings.

    Harnesses without request identity (notably Pi) still produce per-record
    intervals. Aggregate those into a single ``DERIVED`` scope timing so the
    waterfall can render a turn-scope bar instead of ``None``.
    """
    intervals: list[tuple[float, float]] = []
    for record in records:
        if (interval := timing_interval(record.timing)) is not None:
            intervals.append(interval)
    for operation in operations:
        if (interval := timing_interval(operation.timing)) is not None:
            intervals.append(interval)
    if not intervals:
        return None
    start = min(interval[0] for interval in intervals)
    end = max(interval[1] for interval in intervals)
    return Timing(
        start=start,
        end=end,
        duration_ms=(end - start) * 1_000,
        provenance=TimingProvenance.DERIVED,
    )


def _scope_status(requests: tuple[TrajectoryRequest, ...]) -> TrajectoryStatus:
    for statuses in (_FAILED_STATUSES, _ACTIVE_STATUSES):
        if match := next(
            (request.status for request in reversed(requests) if request.status in statuses),
            None,
        ):
            return match
    return requests[-1].status


def _record_scope_status(records: tuple[TrajectoryRecord, ...]) -> TrajectoryStatus:
    activity = tuple(
        record for record in records if record.lane in {TrajectoryLane.MODEL, TrajectoryLane.TOOLS}
    )
    candidates = activity or records
    for statuses in (_FAILED_STATUSES, _ACTIVE_STATUSES):
        if match := next(
            (record.status for record in reversed(candidates) if record.status in statuses),
            None,
        ):
            return match
    return next(
        (
            record.status
            for record in reversed(candidates)
            if record.status is not TrajectoryStatus.UNKNOWN
        ),
        TrajectoryStatus.UNKNOWN,
    )


def _scope_anchor(records: tuple[TrajectoryRecord, ...]) -> str:
    preferred = next(
        (
            record
            for record in records
            if record.lane is TrajectoryLane.MODEL and record.kind is not TrajectoryKind.USAGE
        ),
        None,
    )
    return (preferred or records[0]).record_id


def _scope_has_activity(
    records: tuple[TrajectoryRecord, ...],
    requests: tuple[TrajectoryRequest, ...],
    operations: tuple[TrajectoryToolOperation, ...],
) -> bool:
    return bool(requests or operations) or any(
        record.lane is TrajectoryLane.MODEL and record.kind is not TrajectoryKind.CONTEXT
        for record in records
    )


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
    records: tuple[TrajectoryRecord, ...],
    requests: RequestIndex,
    tools: ToolIndex,
    positions: Mapping[str, int],
) -> tuple[WaterfallProjection, ...]:
    result: list[WaterfallProjection] = []
    grouped_records: OrderedDict[tuple[str, str, str, str], list[TrajectoryRecord]] = OrderedDict()
    record_by_id = {record.record_id: record for record in records}
    for record in records:
        if record.turn_id is None or record.record_id in requests.by_record_id:
            continue
        key = (record.participant_id, record.source_epoch, "turn", record.turn_id)
        grouped_records.setdefault(key, []).append(record)

    grouped_requests: dict[tuple[str, str, str, str], list[TrajectoryRequest]] = {}
    for request in requests.ordered:
        key = _scope_key(request)
        grouped_requests.setdefault(key, []).append(request)
        scope_values = grouped_records.setdefault(key, [])
        known = {record.record_id for record in scope_values}
        for record_id in request.record_ids:
            candidate = record_by_id.get(record_id)
            if candidate is not None and record_id not in known:
                scope_values.append(candidate)
                known.add(record_id)

    ordered_scopes = sorted(
        grouped_records,
        key=lambda key: min(
            (positions.get(record.record_id, len(positions)) for record in grouped_records[key]),
            default=len(positions),
        ),
    )
    for key in ordered_scopes:
        scope_records = tuple(
            sorted(
                grouped_records[key],
                key=lambda record: positions.get(record.record_id, len(positions)),
            )
        )
        if not scope_records:
            continue
        scope_requests = tuple(grouped_requests.get(key, ()))
        operation_ids: list[str] = []
        seen_operations: set[str] = set()
        for record in scope_records:
            operation_id = tools.by_record_id.get(record.record_id)
            if operation_id is not None and operation_id not in seen_operations:
                seen_operations.add(operation_id)
                operation_ids.append(operation_id)
        operations = tuple(
            sorted(
                (tools.by_id[operation_id] for operation_id in operation_ids),
                key=lambda operation: operation_position(operation, positions),
            )
        )
        if not _scope_has_activity(scope_records, scope_requests, operations):
            continue
        member_ids = [record.record_id for record in scope_records]
        for operation in operations:
            member_ids.extend(operation.call_record_ids)
            member_ids.extend(operation.result_record_ids)
        record_ids = tuple(dict.fromkeys(member_ids))
        anchor = (
            request_anchor(scope_requests[0]) if scope_requests else _scope_anchor(scope_records)
        )
        scope_timing = (
            _scope_timing(scope_requests)
            if scope_requests
            else _record_scope_timing(scope_records, operations)
        )
        label = _scope_label(scope_requests, scope_records)
        status = (
            _scope_status(scope_requests) if scope_requests else _record_scope_status(scope_records)
        )
        rows = [
            WaterfallRow(
                key=f"scope:{_scope_id(key)}",
                label=label,
                record_id=anchor,
                member_record_ids=record_ids,
                timing=scope_timing,
                status=status,
                scope=True,
            )
        ]
        by_call = {operation.call_id: operation for operation in operations if operation.call_id}
        for operation in operations[-(WATERFALL_ROWS_PER_SCOPE - 1) :]:
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
                scope_id=_scope_id(key),
                turn_id=key[3] if key[2] == "turn" else None,
                label=label,
                record_ids=record_ids,
                rows=tuple(rows),
                start=min((interval[0] for interval in intervals), default=None),
                end=max((interval[1] for interval in intervals), default=None),
                first_token=scope_timing.first_token if scope_timing is not None else None,
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
