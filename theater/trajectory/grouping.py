"""Deterministic record precedence and turn/step grouping primitives."""

from __future__ import annotations

from collections.abc import Iterable
from json import dumps

from theater.trajectory.models import GroupKind, TrajectoryGroup, TrajectoryRecord


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
    """Order one stream by source coordinates, then make cross-stream ties stable."""
    return tuple(
        sorted(
            deduplicate_records(records),
            key=lambda record: (
                record.source_epoch,
                record.raw_index,
                record.event_ordinal,
                record.record_id,
            ),
        )
    )


def group_records(records: Iterable[TrajectoryRecord]) -> tuple[TrajectoryGroup, ...]:
    """Build Turn → Step groups and an honest Between turns fallback group."""
    ordered = deterministic_record_order(records)
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
    return tuple(group for _position, group in sorted(groups, key=lambda item: item[0]))


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
