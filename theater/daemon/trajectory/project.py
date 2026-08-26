"""Daemon projections from source batches into canonical trajectory records."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from theater.harness.contracts.events import Event
from theater.harness.contracts.source import Batch, HistoryPage
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.trajectory import (
    ContentFormat,
    TrajectoryKind,
    TrajectoryRecord,
    event_to_record,
    fact_to_record,
    merge_records,
)


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


__all__ = [
    "project_batch",
    "project_events_and_facts",
    "project_history_page",
]
