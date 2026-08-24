"""Daemon projections for trajectory capabilities and loaded-scope state."""

from __future__ import annotations

from collections.abc import Iterable

from theater.trajectory import (
    TrajectoryCapabilities,
    TrajectoryCurrentOperation,
    TrajectoryFeature,
    TrajectoryKind,
    TrajectoryLatestError,
    TrajectoryOverview,
    TrajectoryRecord,
    TrajectoryStatus,
    deterministic_record_order,
)


def capabilities_for(
    declared: TrajectoryCapabilities,
    records: Iterable[TrajectoryRecord],
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
    return declared.with_observed(frozenset(observed))


def overview_for(
    records: Iterable[TrajectoryRecord],
    *,
    has_older: bool,
    has_coverage_gaps: bool,
) -> TrajectoryOverview:
    ordered = deterministic_record_order(records)
    current = next(
        (
            _current(record)
            for record in reversed(ordered)
            if record.status
            in {TrajectoryStatus.PENDING, TrajectoryStatus.RUNNING, TrajectoryStatus.PARTIAL}
        ),
        None,
    )
    latest_error = next(
        (
            TrajectoryLatestError(record.record_id, record.summary)
            for record in reversed(ordered)
            if record.kind is TrajectoryKind.ERROR or record.status is TrajectoryStatus.ERROR
        ),
        None,
    )
    usages = tuple(record.usage for record in ordered if record.usage is not None)
    costs = tuple(usage.cost_usd for usage in usages if usage.cost_usd is not None)
    return TrajectoryOverview(
        scope_complete=not has_older and not has_coverage_gaps,
        has_older=has_older,
        has_coverage_gaps=has_coverage_gaps,
        record_count=len(ordered),
        model_operations=sum(record.lane.value == "model" for record in ordered),
        tool_operations=sum(record.kind is TrajectoryKind.TOOL_CALL for record in ordered),
        input_tokens=sum(usage.input_tokens for usage in usages),
        output_tokens=sum(usage.output_tokens for usage in usages),
        cache_read_tokens=sum(usage.cache_read_tokens for usage in usages),
        cache_write_tokens=sum(usage.cache_write_tokens for usage in usages),
        reasoning_tokens=sum(usage.reasoning_tokens for usage in usages),
        reported_cost_usd=sum(costs) if costs else None,
        current=current,
        latest_error=latest_error,
    )


def _current(record: TrajectoryRecord) -> TrajectoryCurrentOperation:
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


__all__ = ["capabilities_for", "overview_for"]
