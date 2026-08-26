"""Delegation, resource, failure, and retry projections."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Mapping

from theater.regie.trajectory.analysis.models import (
    DelegationActivity,
    ProblemActivity,
    ResourceActivity,
    ResourceValues,
)
from theater.regie.trajectory.analysis.waterfall import (
    operation_position,
    request_anchor,
    request_label,
)
from theater.regie.trajectory.constants import TRAJECTORY_INSIGHT_ROW_LIMIT, WATERFALL_MAX_DEPTH
from theater.regie.trajectory.request_rows import RequestIndex
from theater.regie.trajectory.tool_rows import ToolIndex
from theater.trajectory import (
    CostProvenance,
    ParticipantLink,
    TrajectoryRecord,
    TrajectoryRequest,
    TrajectoryStatus,
    TrajectoryUsage,
)

_PROBLEM_STATUSES = frozenset(
    {
        TrajectoryStatus.ERROR,
        TrajectoryStatus.INTERRUPTED,
        TrajectoryStatus.TIMEOUT,
        TrajectoryStatus.CANCELLED,
    }
)


def build_delegations(records: tuple[TrajectoryRecord, ...]) -> tuple[DelegationActivity, ...]:
    grouped: OrderedDict[str, list[tuple[TrajectoryRecord, ParticipantLink]]] = OrderedDict()
    for record in records:
        for link in record.links:
            grouped.setdefault(link.participant_id, []).append((record, link))
    result: list[DelegationActivity] = []
    for participant_id, events in grouped.items():
        latest_record, latest_link = events[-1]
        exact_link = next(
            (link for _record, link in reversed(events) if link.target_record_id is not None),
            latest_link,
        )
        result.append(
            DelegationActivity(
                participant_id=participant_id,
                directions=frozenset(link.direction for _record, link in events),
                relations=tuple(dict.fromkeys(link.relation for _record, link in events)),
                event_count=len(events),
                record_ids=tuple(dict.fromkeys(record.record_id for record, _link in events)),
                latest_record_id=latest_record.record_id,
                latest_summary=latest_record.summary,
                latest_status=latest_record.status,
                target=exact_link,
            )
        )
    return tuple(result[:TRAJECTORY_INSIGHT_ROW_LIMIT])


def _usage_values(usage: TrajectoryUsage) -> ResourceValues:
    return ResourceValues(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        cache_tokens=usage.cache_read_tokens + usage.cache_write_tokens,
        cost_usd=usage.cost_usd,
        cost_provenance=usage.cost_provenance.value,
        cost_complete=usage.cost_usd is not None,
    )


def _sum_usage(usages: Iterable[TrajectoryUsage]) -> ResourceValues:
    values = tuple(usages)
    known_costs = [usage.cost_usd for usage in values if usage.cost_usd is not None]
    provenances = {usage.cost_provenance for usage in values if usage.cost_usd is not None}
    if not provenances:
        provenance = CostProvenance.UNKNOWN.value
    elif len(provenances) == 1:
        provenance = next(iter(provenances)).value
    else:
        provenance = "mixed"
    return ResourceValues(
        input_tokens=sum(usage.input_tokens for usage in values),
        output_tokens=sum(usage.output_tokens for usage in values),
        reasoning_tokens=sum(usage.reasoning_tokens for usage in values),
        cache_tokens=sum(usage.cache_read_tokens + usage.cache_write_tokens for usage in values),
        cost_usd=sum(known_costs) if known_costs else None,
        cost_provenance=provenance,
        cost_complete=bool(values) and len(known_costs) == len(values),
    )


def build_resources(requests: RequestIndex) -> tuple[ResourceActivity, ...]:
    grouped: OrderedDict[tuple[str, str], list[TrajectoryRequest]] = OrderedDict()
    for request in requests.ordered:
        if request.usage is None:
            continue
        turn = request.turn_id or f"request:{request.request_id}"
        grouped.setdefault((request.source_epoch, turn), []).append(request)
    result: list[ResourceActivity] = []
    for (epoch, turn), turn_requests in grouped.items():
        record_ids = tuple(
            dict.fromkeys(
                record_id for request in turn_requests for record_id in request.record_ids
            )
        )
        result.append(
            ResourceActivity(
                key=f"turn:{epoch}:{turn}",
                scope="turn",
                label=f"Turn {_short_identity(turn)}",
                model=None,
                values=_sum_usage(request.usage for request in turn_requests if request.usage),
                record_ids=record_ids,
                record_id=request_anchor(turn_requests[0]),
            )
        )
        for request in turn_requests:
            usage = request.usage
            if usage is None:
                continue
            result.append(
                ResourceActivity(
                    key=request.request_id,
                    scope="request",
                    label=_short_identity(request.source_request_id or request.request_id),
                    model=request_label(request),
                    values=_usage_values(usage),
                    record_ids=request.record_ids,
                    record_id=request_anchor(request),
                    depth=1,
                )
            )
    return tuple(result[-TRAJECTORY_INSIGHT_ROW_LIMIT:])


def _retry_depth(target: str | None, by_id: Mapping[str, TrajectoryRecord]) -> int:
    depth = 0
    seen: set[str] = set()
    while target is not None and target not in seen and depth < WATERFALL_MAX_DEPTH:
        seen.add(target)
        depth += 1
        record = by_id.get(target)
        target = record.retry_of_record_id if record is not None else None
    return depth


def build_problems(
    records: tuple[TrajectoryRecord, ...],
    tools: ToolIndex,
    positions: Mapping[str, int],
) -> tuple[ProblemActivity, ...]:
    tool_members = frozenset(tools.by_record_id)
    by_id = {record.record_id: record for record in records}
    rows: list[tuple[int, ProblemActivity]] = []
    for operation in tools.ordered:
        if (
            operation.failure is None
            and operation.retry_of_record_id is None
            and operation.status not in _PROBLEM_STATUSES
        ):
            continue
        members = (*operation.call_record_ids, *operation.result_record_ids)
        anchor = (operation.call_record_ids or operation.result_record_ids)[0]
        rows.append(
            (
                operation_position(operation, positions),
                ProblemActivity(
                    record_id=anchor,
                    member_record_ids=members,
                    label=operation.tool_name or "unknown tool",
                    status=operation.status,
                    failure=operation.failure,
                    retry_of_record_id=operation.retry_of_record_id,
                    retry_attempt=operation.retry_attempt,
                    chain_depth=_retry_depth(operation.retry_of_record_id, by_id),
                ),
            )
        )
    for position, record in enumerate(records):
        if record.record_id in tool_members or (
            record.failure is None
            and record.retry_of_record_id is None
            and record.status not in _PROBLEM_STATUSES
        ):
            continue
        rows.append(
            (
                position,
                ProblemActivity(
                    record_id=record.record_id,
                    member_record_ids=(record.record_id,),
                    label=record.summary or record.kind.value.replace("_", " "),
                    status=record.status,
                    failure=record.failure,
                    retry_of_record_id=record.retry_of_record_id,
                    retry_attempt=record.retry_attempt,
                    chain_depth=_retry_depth(record.retry_of_record_id, by_id),
                ),
            )
        )
    rows.sort(key=lambda item: (item[0], item[1].record_id))
    return tuple(row for _position, row in rows[-TRAJECTORY_INSIGHT_ROW_LIMIT:])


def _short_identity(value: str) -> str:
    if len(value) <= 20:
        return value
    return f"{value[:8]}…{value[-8:]}"


__all__ = ["build_delegations", "build_problems", "build_resources"]
