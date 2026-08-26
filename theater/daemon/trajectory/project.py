"""Daemon projections from source batches into canonical trajectory records."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from theater.daemon.trajectory.mcp_projection import classify_mcp_fact
from theater.harness.contracts.events import Event, EventKind
from theater.harness.contracts.source import Batch, HistoryPage
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.pricing import estimate_cost_usd
from theater.trajectory import (
    ContentFormat,
    CostProvenance,
    DetailField,
    Timing,
    TimingProvenance,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryStatus,
    TrajectoryUsage,
    merge_records,
)
from theater.trajectory.identity import fallback_record_id, namespaced_native_id


def project_batch(
    batch: Batch,
    *,
    participant_id: str,
    source_epoch: str,
    source: str = "baseline",
) -> tuple[TrajectoryRecord, ...]:
    """Project a live batch, allowing rich facts to replace baseline events."""
    return project_events_and_facts(
        batch.events if batch.trajectory_events is None else batch.trajectory_events,
        batch.trajectory,
        participant_id=participant_id,
        source_epoch=source_epoch,
        source=source,
    )


def project_history_page(
    page: HistoryPage,
    *,
    participant_id: str,
    source_epoch: str,
    source: str = "baseline",
) -> tuple[TrajectoryRecord, ...]:
    """Project one bounded history page without changing a live source cursor."""
    return project_events_and_facts(
        page.events if page.trajectory_events is None else page.trajectory_events,
        page.trajectory,
        participant_id=participant_id,
        source_epoch=source_epoch,
        source=source,
    )


def project_events_and_facts(
    events: Iterable[Event],
    facts: Iterable[TrajectoryFact],
    *,
    participant_id: str,
    source_epoch: str,
    source: str = "baseline",
) -> tuple[TrajectoryRecord, ...]:
    """Merge rich source facts over their matching normalized Event outputs."""
    baseline_events = tuple(event for event in events if not event.usage_only)
    rich_facts = tuple(facts)
    baseline = tuple(
        event_to_record(
            event,
            participant_id=participant_id,
            source_epoch=source_epoch,
            event_ordinal=ordinal,
            source=source,
        )
        for event, ordinal in _event_ordinals(baseline_events)
    )
    rich = tuple(
        fact_to_record(
            fact,
            participant_id=participant_id,
            source_epoch=source_epoch,
            source=fact.source,
        )
        for fact in rich_facts
    )
    retained, enriched = _merge_replaced_baseline(baseline, rich, rich_facts)
    return merge_records((), (*retained, *enriched))


def record_id_for_fact(fact: TrajectoryFact, source_epoch: str) -> str:
    """Namespace local native ids and use source coordinates for fallbacks."""
    if fact.native_id is not None:
        return namespaced_native_id(fact.native_id, source_epoch)
    return fallback_record_id(
        source_epoch,
        fact.raw_index,
        fact.event_ordinal,
        source_offset=fact.source_offset,
    )


def event_to_fact(
    event: Event,
    *,
    source: str = "baseline",
    event_ordinal: int = 0,
) -> TrajectoryFact:
    """Project one normalized control event without assigning a participant."""
    kind, lane = _kind_and_lane(event.kind)
    status = TrajectoryStatus.ERROR if event.kind is EventKind.ERROR else TrajectoryStatus.COMPLETED
    timing = (
        Timing(start=event.ts, provenance=TimingProvenance.SOURCE) if event.ts is not None else None
    )
    usage = _usage(event)
    details: list[DetailField] = []
    if event.tool_name:
        details.append(DetailField.from_text("tool", event.tool_name, format=ContentFormat.TEXT))
    details.extend(
        DetailField.from_text(f"path.{path.mode}", path.path, format=ContentFormat.PATH)
        for path in event.paths
    )
    if event.raw_text is not None and event.raw_text != event.text:
        details.append(DetailField.from_text("raw", event.raw_text, format=ContentFormat.TEXT))
    return TrajectoryFact(
        kind=kind,
        lane=lane,
        source=source,
        summary=event.text,
        status=status,
        raw_index=event.raw_index,
        source_offset=event.source_offset,
        event_ordinal=event_ordinal,
        turn_id=event.turn_id,
        timing=timing,
        usage=usage,
        details=tuple(details),
    )


def fact_to_record(
    fact: TrajectoryFact,
    *,
    participant_id: str,
    source_epoch: str,
    source: str | None = None,
    links: Iterable = (),
) -> TrajectoryRecord:
    """Add daemon-supplied participant identity to a source-local fact."""
    fact = classify_mcp_fact(fact)
    lane = fact.lane or _lane_for_kind(fact.kind)
    return TrajectoryRecord(
        record_id=record_id_for_fact(fact, source_epoch),
        revision=fact.revision,
        participant_id=participant_id,
        source_epoch=source_epoch,
        lane=lane,
        kind=fact.kind,
        source=source or fact.source,
        summary=fact.summary,
        status=fact.status,
        native_id=(
            namespaced_native_id(fact.native_id, source_epoch)
            if fact.native_id is not None
            else None
        ),
        raw_index=fact.raw_index,
        source_offset=fact.source_offset,
        event_ordinal=fact.event_ordinal,
        turn_id=fact.turn_id,
        step_id=fact.step_id,
        request_id=fact.request_id,
        call_id=fact.call_id,
        parent_call_id=fact.parent_call_id,
        mcp_server=fact.mcp_server,
        mcp_tool=fact.mcp_tool,
        links=tuple(links),
        timing=fact.timing,
        usage=_priced_usage(fact.usage),
        failure=fact.failure,
        retry_of_record_id=(
            namespaced_native_id(fact.retry_of_native_id, source_epoch)
            if fact.retry_of_native_id is not None
            else None
        ),
        retry_attempt=fact.retry_attempt,
        details=fact.details,
    )


def event_to_record(
    event: Event,
    *,
    participant_id: str,
    source_epoch: str,
    event_ordinal: int = 0,
    source: str = "baseline",
) -> TrajectoryRecord:
    """Project one normalized Event into a canonical trajectory record."""
    return fact_to_record(
        event_to_fact(event, source=source, event_ordinal=event_ordinal),
        participant_id=participant_id,
        source_epoch=source_epoch,
    )


def project_events(
    events: Iterable[Event],
    *,
    participant_id: str,
    source_epoch: str,
    source: str = "baseline",
) -> tuple[TrajectoryRecord, ...]:
    """Project events while assigning ordinals only within each raw record."""
    ordinals: dict[int, int] = {}
    records: list[TrajectoryRecord] = []
    for event in events:
        coordinate = event.source_offset if event.source_offset is not None else event.raw_index
        ordinal = ordinals.get(coordinate, 0)
        ordinals[coordinate] = ordinal + 1
        records.append(
            event_to_record(
                event,
                participant_id=participant_id,
                source_epoch=source_epoch,
                event_ordinal=ordinal,
                source=source,
            )
        )
    return tuple(records)


def project_facts(
    facts: Iterable[TrajectoryFact],
    *,
    participant_id: str,
    source_epoch: str,
    source: str | None = None,
) -> tuple[TrajectoryRecord, ...]:
    """Project rich facts while leaving participant-link resolution to the daemon."""
    return tuple(
        fact_to_record(
            fact, participant_id=participant_id, source_epoch=source_epoch, source=source
        )
        for fact in facts
    )


def _event_ordinals(events: tuple[Event, ...]) -> tuple[tuple[Event, int], ...]:
    ordinals: dict[int, int] = {}
    result: list[tuple[Event, int]] = []
    for event in events:
        coordinate = event.source_offset if event.source_offset is not None else event.raw_index
        ordinal = ordinals.get(coordinate, 0)
        ordinals[coordinate] = ordinal + 1
        result.append((event, ordinal))
    return tuple(result)


def _merge_replaced_baseline(
    baseline: tuple[TrajectoryRecord, ...],
    rich: tuple[TrajectoryRecord, ...],
    facts: tuple[TrajectoryFact, ...],
) -> tuple[tuple[TrajectoryRecord, ...], tuple[TrajectoryRecord, ...]]:
    used: set[str] = set()
    baseline_ids = frozenset(record.record_id for record in baseline)
    enriched: list[TrajectoryRecord] = []
    for fact, rich_record in zip(facts, rich, strict=True):
        enriched_record = rich_record
        candidates = [
            record
            for record in baseline
            if record.record_id not in used and _same_coordinate(record, fact)
        ]
        if len(candidates) > 1:
            same_kind = [record for record in candidates if _same_kind(record, fact.kind)]
            candidates = same_kind or candidates
        if candidates:
            baseline_record = candidates[0]
            used.add(baseline_record.record_id)
            rich_paths = {
                (detail.name, detail.preview.text)
                for detail in rich_record.details
                if detail.format is ContentFormat.PATH
            }
            path_details = tuple(
                detail
                for detail in baseline_record.details
                if detail.format is ContentFormat.PATH
                and (detail.name, detail.preview.text) not in rich_paths
            )
            if path_details:
                enriched_record = replace(
                    rich_record, details=(*rich_record.details, *path_details)
                )
        elif rich_record.record_id in baseline_ids:
            used.add(rich_record.record_id)
        enriched.append(enriched_record)
    retained = tuple(record for record in baseline if record.record_id not in used)
    return retained, tuple(enriched)


def _same_coordinate(record: TrajectoryRecord, fact: TrajectoryFact) -> bool:
    if record.source_offset is not None or fact.source_offset is not None:
        return record.source_offset is not None and record.source_offset == fact.source_offset
    return record.raw_index == fact.raw_index and record.event_ordinal == fact.event_ordinal


def _same_kind(record: TrajectoryRecord, kind: TrajectoryKind) -> bool:
    return record.kind is kind


def _kind_and_lane(kind: EventKind) -> tuple[TrajectoryKind, TrajectoryLane]:
    mapping = {
        EventKind.USER: (TrajectoryKind.USER, TrajectoryLane.INPUT),
        EventKind.ASSISTANT: (TrajectoryKind.ASSISTANT, TrajectoryLane.MODEL),
        EventKind.TOOL_CALL: (TrajectoryKind.TOOL_CALL, TrajectoryLane.TOOLS),
        EventKind.TOOL_RESULT: (TrajectoryKind.TOOL_RESULT, TrajectoryLane.TOOLS),
        EventKind.ERROR: (TrajectoryKind.ERROR, TrajectoryLane.THEATER),
    }
    return mapping[kind]


def _lane_for_kind(kind: TrajectoryKind) -> TrajectoryLane:
    if kind is TrajectoryKind.USER:
        return TrajectoryLane.INPUT
    if kind in (
        TrajectoryKind.ASSISTANT,
        TrajectoryKind.REASONING,
        TrajectoryKind.USAGE,
        TrajectoryKind.SYSTEM,
        TrajectoryKind.CONTEXT,
    ):
        return TrajectoryLane.MODEL
    if kind in (TrajectoryKind.TOOL_CALL, TrajectoryKind.TOOL_RESULT):
        return TrajectoryLane.TOOLS
    return TrajectoryLane.THEATER


def _usage(event: Event) -> TrajectoryUsage | None:
    if event.usage is None:
        return None
    return TrajectoryUsage(
        model=event.usage.model,
        provider=event.usage.provider,
        request_id=event.usage.idempotency_key,
        input_tokens=event.usage.input_tokens,
        output_tokens=event.usage.output_tokens,
        reasoning_tokens=event.usage.reasoning_output_tokens,
        cache_read_tokens=event.usage.cache_read_input_tokens,
        cache_write_tokens=event.usage.cache_creation_input_tokens,
        cost_usd=event.usage.cost_usd,
        cost_provenance=event.usage.cost_provenance,
    )


def _priced_usage(usage: TrajectoryUsage | None) -> TrajectoryUsage | None:
    if usage is None or usage.cost_usd is not None:
        return usage
    if not any(
        (
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            usage.cache_write_tokens,
            usage.reasoning_tokens,
        )
    ):
        return usage
    cost = estimate_cost_usd(
        usage.model,
        provider=usage.provider,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        reasoning_tokens=usage.reasoning_tokens,
    )
    if cost is None:
        return usage
    return replace(usage, cost_usd=cost, cost_provenance=CostProvenance.ESTIMATED)


__all__ = [
    "event_to_fact",
    "event_to_record",
    "fact_to_record",
    "project_batch",
    "project_events",
    "project_events_and_facts",
    "project_facts",
    "project_history_page",
    "record_id_for_fact",
]
