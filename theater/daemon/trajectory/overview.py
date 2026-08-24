"""Daemon projections for trajectory capabilities and loaded-scope state."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from theater.constants.trajectory import (
    TRAJECTORY_OVERVIEW_MAX_COST_USD,
    TRAJECTORY_OVERVIEW_MAX_COUNT,
    TRAJECTORY_OVERVIEW_MAX_TOKENS,
)
from theater.trajectory import (
    TrajectoryCapabilities,
    TrajectoryCurrentOperation,
    TrajectoryFeature,
    TrajectoryIncompleteReason,
    TrajectoryKind,
    TrajectoryOverview,
    TrajectoryProblem,
    TrajectoryRecord,
    TrajectoryStatus,
    deterministic_record_order,
    requests_for_records,
)

_ACTIVE = frozenset({TrajectoryStatus.PENDING, TrajectoryStatus.RUNNING, TrajectoryStatus.PARTIAL})
_TERMINAL = frozenset(
    {
        TrajectoryStatus.COMPLETED,
        TrajectoryStatus.ERROR,
        TrajectoryStatus.INTERRUPTED,
        TrajectoryStatus.TIMEOUT,
        TrajectoryStatus.CANCELLED,
    }
)
_PROBLEM_KINDS = frozenset(
    {TrajectoryKind.ERROR, TrajectoryKind.JOB_FAILURE, TrajectoryKind.OBSERVATION_ERROR}
)
_PROBLEM_STATUSES = frozenset(
    {
        TrajectoryStatus.ERROR,
        TrajectoryStatus.INTERRUPTED,
        TrajectoryStatus.TIMEOUT,
        TrajectoryStatus.CANCELLED,
    }
)


@dataclass(frozen=True, slots=True)
class TrajectoryResponseValues:
    capabilities: TrajectoryCapabilities
    overview: TrajectoryOverview


def capabilities_for(
    declared: TrajectoryCapabilities,
    records: Iterable[TrajectoryRecord],
    *,
    live_updates_observed: bool,
) -> TrajectoryCapabilities:
    return _capabilities(declared, tuple(records), live_updates_observed=live_updates_observed)


def overview_for(
    records: Iterable[TrajectoryRecord],
    *,
    has_older: bool,
    has_coverage_gaps: bool,
    cache_evicted: bool = False,
) -> TrajectoryOverview:
    return _overview(
        deterministic_record_order(records),
        has_older=has_older,
        has_coverage_gaps=has_coverage_gaps,
        cache_evicted=cache_evicted,
    )


def response_values_for(
    declared: TrajectoryCapabilities,
    records: Iterable[TrajectoryRecord],
    *,
    live_updates_observed: bool,
    has_older: bool,
    has_coverage_gaps: bool,
    cache_evicted: bool,
) -> TrajectoryResponseValues:
    ordered = deterministic_record_order(records)
    return TrajectoryResponseValues(
        _capabilities(declared, ordered, live_updates_observed=live_updates_observed),
        _overview(
            ordered,
            has_older=has_older,
            has_coverage_gaps=has_coverage_gaps,
            cache_evicted=cache_evicted,
        ),
    )


def _capabilities(
    declared: TrajectoryCapabilities,
    records: tuple[TrajectoryRecord, ...],
    *,
    live_updates_observed: bool,
) -> TrajectoryCapabilities:
    observed: set[TrajectoryFeature] = set()
    for record in records:
        if record.kind in {TrajectoryKind.TOOL_CALL, TrajectoryKind.TOOL_RESULT}:
            observed.add(TrajectoryFeature.TOOLS)
        if record.kind is TrajectoryKind.REASONING:
            observed.add(TrajectoryFeature.REASONING)
        if record.kind is TrajectoryKind.CONTEXT:
            observed.add(TrajectoryFeature.CONTEXT)
        if record.timing is not None and any(
            value is not None
            for value in (record.timing.start, record.timing.end, record.timing.duration_ms)
        ):
            observed.add(TrajectoryFeature.TIMING)
        if record.usage is not None:
            observed.add(TrajectoryFeature.USAGE)
            if record.usage.model is not None:
                observed.add(TrajectoryFeature.MODELS)
            if record.usage.request_id is not None:
                observed.add(TrajectoryFeature.REQUESTS)
    if live_updates_observed:
        observed.add(TrajectoryFeature.LIVE_UPDATES)
    return TrajectoryCapabilities(
        supported=declared.supported,
        unsupported=declared.unsupported,
        observed=frozenset(observed),
    )


def _overview(
    ordered: tuple[TrajectoryRecord, ...],
    *,
    has_older: bool,
    has_coverage_gaps: bool,
    cache_evicted: bool,
) -> TrajectoryOverview:
    usage_records = _usage_records(ordered)
    usages = tuple(record.usage for record in usage_records if record.usage is not None)
    input_tokens, input_saturated = _sum_int(usage.input_tokens for usage in usages)
    output_tokens, output_saturated = _sum_int(usage.output_tokens for usage in usages)
    cache_read_tokens, cache_read_saturated = _sum_int(usage.cache_read_tokens for usage in usages)
    cache_write_tokens, cache_write_saturated = _sum_int(
        usage.cache_write_tokens for usage in usages
    )
    reasoning_tokens, reasoning_saturated = _sum_int(usage.reasoning_tokens for usage in usages)
    reported_cost_usd, cost_saturated = _sum_cost(usage.cost_usd for usage in usages)
    reasons = tuple(
        reason
        for reason, present in (
            (TrajectoryIncompleteReason.OLDER_HISTORY, has_older),
            (TrajectoryIncompleteReason.COVERAGE_GAPS, has_coverage_gaps),
            (TrajectoryIncompleteReason.CACHE_EVICTED, cache_evicted),
        )
        if present
    )
    record_count, record_count_saturated = _bounded_count(len(ordered))
    model_operations, model_operations_saturated = _bounded_count(
        len(requests_for_records(ordered))
    )
    tool_operations, tool_operations_saturated = _bounded_count(
        sum(record.kind is TrajectoryKind.TOOL_CALL for record in ordered)
    )
    return TrajectoryOverview(
        incomplete_reasons=reasons,
        record_count=record_count,
        model_operations=model_operations,
        tool_operations=tool_operations,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=reasoning_tokens,
        reported_cost_usd=reported_cost_usd,
        totals_saturated=any(
            (
                input_saturated,
                output_saturated,
                cache_read_saturated,
                cache_write_saturated,
                reasoning_saturated,
                cost_saturated,
                record_count_saturated,
                model_operations_saturated,
                tool_operations_saturated,
            )
        ),
        current=_current(ordered),
        latest_problem=_latest_problem(ordered),
    )


def _usage_records(ordered: tuple[TrajectoryRecord, ...]) -> tuple[TrajectoryRecord, ...]:
    requested: dict[tuple[str, str], TrajectoryRecord] = {}
    independent: list[TrajectoryRecord] = []
    for record in ordered:
        if record.usage is None:
            continue
        if record.usage.request_id is None:
            independent.append(record)
        else:
            requested[(record.source_epoch, record.usage.request_id)] = record
    return (*independent, *requested.values())


def _current(ordered: tuple[TrajectoryRecord, ...]) -> TrajectoryCurrentOperation | None:
    closed_calls: set[tuple[str, str, str]] = set()
    for record in reversed(ordered):
        family = _call_family(record.kind)
        if (
            family is not None
            and record.kind
            in {
                TrajectoryKind.TOOL_RESULT,
                TrajectoryKind.AWAIT_END,
            }
            and record.status in _TERMINAL
        ):
            if record.call_id is not None:
                closed_calls.add((family, record.source_epoch, record.call_id))
            continue
        if record.status not in _ACTIVE:
            continue
        if (
            family is not None
            and record.kind in {TrajectoryKind.TOOL_CALL, TrajectoryKind.AWAIT_START}
            and record.call_id is not None
            and (family, record.source_epoch, record.call_id) in closed_calls
        ):
            continue
        timing = record.timing
        return TrajectoryCurrentOperation(
            record_id=record.record_id,
            kind=record.kind,
            lane=record.lane,
            status=record.status,
            summary=record.summary,
            model=record.usage.model if record.usage is not None else None,
            start=timing.start if timing is not None else None,
            duration_ms=timing.duration_ms if timing is not None else None,
        )
    return None


def _latest_problem(ordered: tuple[TrajectoryRecord, ...]) -> TrajectoryProblem | None:
    for record in reversed(ordered):
        if record.kind in _PROBLEM_KINDS or record.status in _PROBLEM_STATUSES:
            return TrajectoryProblem(record.record_id, record.summary)
    return None


def _call_family(kind: TrajectoryKind) -> str | None:
    if kind in {TrajectoryKind.TOOL_CALL, TrajectoryKind.TOOL_RESULT}:
        return "tool"
    if kind in {TrajectoryKind.AWAIT_START, TrajectoryKind.AWAIT_END}:
        return "await"
    return None


def _bounded_count(value: int) -> tuple[int, bool]:
    return (
        (TRAJECTORY_OVERVIEW_MAX_COUNT, True)
        if value > TRAJECTORY_OVERVIEW_MAX_COUNT
        else (value, False)
    )


def _sum_int(values: Iterable[int]) -> tuple[int, bool]:
    total = 0
    for value in values:
        if total > TRAJECTORY_OVERVIEW_MAX_TOKENS - value:
            return TRAJECTORY_OVERVIEW_MAX_TOKENS, True
        total += value
    return total, False


def _sum_cost(values: Iterable[float | None]) -> tuple[float | None, bool]:
    total = 0.0
    seen = False
    for value in values:
        if value is None:
            continue
        seen = True
        if not math.isfinite(value) or total > TRAJECTORY_OVERVIEW_MAX_COST_USD - value:
            return TRAJECTORY_OVERVIEW_MAX_COST_USD, True
        total += value
    return (total if seen else None), False


__all__ = ["TrajectoryResponseValues", "capabilities_for", "overview_for", "response_values_for"]
