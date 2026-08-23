"""Deterministic record precedence and turn/step grouping primitives."""

from __future__ import annotations

from collections.abc import Iterable
from json import dumps

from theater.trajectory.enums import GroupKind, TimingProvenance
from theater.trajectory.page import TrajectoryGroup
from theater.trajectory.records import TrajectoryRecord


def newer_record(current: TrajectoryRecord, candidate: TrajectoryRecord) -> TrajectoryRecord:
    """Choose by revision, with a deterministic tie-break for malformed duplicates."""
    if candidate.revision > current.revision:
        return candidate
    if candidate.revision < current.revision:
        return current
    if candidate.to_wire() == current.to_wire():
        return current
    current_key = dumps(current.to_wire(), sort_keys=True, separators=(",", ":"))
    candidate_key = dumps(candidate.to_wire(), sort_keys=True, separators=(",", ":"))
    return candidate if candidate_key > current_key else current


def deduplicate_records(records: Iterable[TrajectoryRecord]) -> tuple[TrajectoryRecord, ...]:
    """Deduplicate by exact record identity and retain deterministic input order."""
    selected: dict[str, TrajectoryRecord] = {}
    order: list[str] = []
    for record in records:
        if not isinstance(record, TrajectoryRecord):
            raise TypeError("records must contain TrajectoryRecord values")
        if record.record_id not in selected:
            order.append(record.record_id)
            selected[record.record_id] = record
        else:
            selected[record.record_id] = newer_record(selected[record.record_id], record)
    return tuple(selected[record_id] for record_id in order)


def merge_records(
    existing: Iterable[TrajectoryRecord], incoming: Iterable[TrajectoryRecord]
) -> tuple[TrajectoryRecord, ...]:
    """Apply incoming upserts without allowing older revisions to rewind state."""
    return deduplicate_records((*existing, *incoming))


def deterministic_record_order(records: Iterable[TrajectoryRecord]) -> tuple[TrajectoryRecord, ...]:
    """Sort source coordinates within each stream without ordering streams lexically."""
    streams: dict[str, list[TrajectoryRecord]] = {}
    stream_order: list[str] = []
    for record in deduplicate_records(records):
        if record.source_epoch not in streams:
            streams[record.source_epoch] = []
            stream_order.append(record.source_epoch)
        streams[record.source_epoch].append(record)
    ordered: list[TrajectoryRecord] = []
    for source_epoch in stream_order:
        ordered.extend(
            sorted(
                streams[source_epoch],
                key=lambda record: (record.raw_index, record.event_ordinal, record.record_id),
            )
        )
    return tuple(ordered)


def group_records(records: Iterable[TrajectoryRecord]) -> tuple[TrajectoryGroup, ...]:
    """Build Turn → Step groups and an honest Between turns fallback group."""
    ordered = deterministic_record_order(records)
    cross_stream = len({record.source_epoch for record in ordered}) > 1
    if cross_stream and not _positionable_streams(ordered):
        return (_between_group(ordered),) if ordered else ()
    turn_entries: dict[str, list[TrajectoryRecord]] = {}
    turn_order: list[str] = []
    turn_first: dict[str, int] = {}
    between: list[TrajectoryRecord] = []
    between_first: int | None = None
    for position, record in enumerate(ordered):
        if record.turn_id is None:
            if between_first is None:
                between_first = position
            between.append(record)
            continue
        if record.turn_id not in turn_entries:
            turn_entries[record.turn_id] = []
            turn_order.append(record.turn_id)
            turn_first[record.turn_id] = position
        turn_entries[record.turn_id].append(record)

    groups: list[tuple[int, TrajectoryGroup]] = []
    if between:
        groups.append(
            (
                between_first if between_first is not None else 0,
                TrajectoryGroup(
                    group_id="between-turns",
                    kind=GroupKind.BETWEEN_TURNS,
                    label="Between turns",
                    record_ids=tuple(record.record_id for record in between),
                ),
            )
        )
    for turn_id in turn_order:
        turn_records = turn_entries[turn_id]
        children = _step_groups(turn_records, turn_id)
        direct = tuple(record.record_id for record in turn_records if record.step_id is None)
        groups.append(
            (
                turn_first[turn_id],
                TrajectoryGroup(
                    group_id=f"turn:{turn_id}",
                    kind=GroupKind.TURN,
                    label=f"Turn {turn_id}",
                    record_ids=direct,
                    children=children,
                    turn_id=turn_id,
                ),
            )
        )
    grouped = tuple(group for _position, group in sorted(groups, key=lambda item: item[0]))
    if cross_stream and _reliable_boundary_times(grouped, ordered):
        return tuple(
            group
            for _time, _position, group in sorted(
                (
                    (_group_time(group, ordered), position, group)
                    for position, group in enumerate(grouped)
                ),
                key=lambda item: (item[0], item[1]),
            )
        )
    return grouped


def _between_group(records: Iterable[TrajectoryRecord]) -> TrajectoryGroup:
    values = tuple(records)
    return TrajectoryGroup(
        group_id="between-turns",
        kind=GroupKind.BETWEEN_TURNS,
        label="Between turns",
        record_ids=tuple(record.record_id for record in values),
    )


def _positionable_streams(records: tuple[TrajectoryRecord, ...]) -> bool:
    call_ids = {record.call_id for record in records if record.call_id is not None}
    if any(record.parent_call_id in call_ids for record in records):
        return True
    return all(
        record.timing is not None
        and record.timing.start is not None
        and record.timing.provenance in (TimingProvenance.SOURCE, TimingProvenance.OBSERVED)
        for record in records
    )


def _reliable_boundary_times(
    groups: tuple[TrajectoryGroup, ...], records: tuple[TrajectoryRecord, ...]
) -> bool:
    if not groups:
        return False
    by_id = {record.record_id: record for record in records}
    return all(_group_time(group, records) is not None for group in groups) and all(
        record.timing is not None
        and record.timing.start is not None
        and record.timing.provenance in (TimingProvenance.SOURCE, TimingProvenance.OBSERVED)
        for record in by_id.values()
    )


def _group_time(group: TrajectoryGroup, records: tuple[TrajectoryRecord, ...]) -> float | None:
    by_id = {record.record_id: record for record in records}
    record_ids = list(group.record_ids)
    for child in group.children:
        record_ids.extend(child.record_ids)
    times: list[float] = []
    for record_id in record_ids:
        record = by_id.get(record_id)
        if record is None or record.timing is None or record.timing.start is None:
            continue
        times.append(record.timing.start)
    return min(times) if times else None


def _step_groups(records: Iterable[TrajectoryRecord], turn_id: str) -> tuple[TrajectoryGroup, ...]:
    entries: dict[str, list[TrajectoryRecord]] = {}
    order: list[str] = []
    for record in records:
        if record.step_id is None:
            continue
        if record.step_id not in entries:
            entries[record.step_id] = []
            order.append(record.step_id)
        entries[record.step_id].append(record)
    return tuple(
        TrajectoryGroup(
            group_id=f"step:{turn_id}:{step_id}",
            kind=GroupKind.STEP,
            label=f"Step {step_id}",
            record_ids=tuple(record.record_id for record in entries[step_id]),
            step_id=step_id,
            turn_id=turn_id,
        )
        for step_id in order
    )


__all__ = [
    "deduplicate_records",
    "deterministic_record_order",
    "group_records",
    "merge_records",
    "newer_record",
]
