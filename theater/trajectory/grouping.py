"""Deterministic record precedence and turn/step grouping primitives."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from hashlib import sha256
from json import dumps

from theater.constants.trajectory import (
    TRAJECTORY_IDENTIFIER_MAX_BYTES,
    TRAJECTORY_MAX_GROUP_CHILDREN,
    TRAJECTORY_MAX_GROUP_RECORD_IDS,
    TRAJECTORY_THEATER_BUS_RECORD_PREFIX,
)
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
                key=lambda record: (
                    record.source_offset if record.source_offset is not None else record.raw_index,
                    record.event_ordinal,
                    record.record_id,
                ),
            )
        )
    return tuple(ordered)


def group_records(records: Iterable[TrajectoryRecord]) -> tuple[TrajectoryGroup, ...]:
    """Build turn/step groups without inventing cross-stream chronology."""
    ordered = deterministic_record_order(records)
    if not ordered:
        return ()
    positions = {record.record_id: position for position, record in enumerate(ordered)}
    turn_entries: dict[tuple[str, str], list[TrajectoryRecord]] = {}
    turn_order: list[tuple[str, str]] = []
    turn_first: dict[tuple[str, str], int] = {}
    record_step: dict[str, str | None] = {}
    for position, record in enumerate(ordered):
        if record.turn_id is None:
            continue
        key = (record.source_epoch, record.turn_id)
        _ensure_turn(turn_entries, turn_order, turn_first, key, position)
        turn_entries[key].append(record)
        record_step[record.record_id] = record.step_id

    call_targets: dict[str, tuple[tuple[str, str], str | None] | None] = {}
    for record in ordered:
        if record.call_id is None or record.turn_id is None:
            continue
        call_target = ((record.source_epoch, record.turn_id), record.step_id)
        if record.call_id in call_targets and call_targets[record.call_id] != call_target:
            call_targets[record.call_id] = None
        else:
            call_targets[record.call_id] = call_target

    unpositioned: list[tuple[int, TrajectoryRecord]] = []
    for position, record in enumerate(ordered):
        if record.turn_id is not None:
            continue
        linked_target: tuple[tuple[str, str], str | None] | None = None
        if record.parent_call_id is not None:
            linked_target = call_targets.get(record.parent_call_id)
        if linked_target is None:
            unpositioned.append((position, record))
            continue
        turn_key, parent_step = linked_target
        _ensure_turn(turn_entries, turn_order, turn_first, turn_key, position)
        turn_entries[turn_key].append(record)
        record_step[record.record_id] = record.step_id or parent_step

    groups: list[tuple[float, TrajectoryGroup]] = []
    for turn_key in turn_order:
        turn_records = turn_entries[turn_key]
        groups.extend(
            (float(turn_first[turn_key]), group)
            for group in _turn_groups(turn_key, turn_records, record_step, positions)
        )

    between_groups = _between_groups(unpositioned, turn_order, turn_entries, turn_first)
    groups.extend(between_groups)
    return tuple(group for _position, group in sorted(groups, key=lambda item: item[0]))


def _ensure_turn(
    entries: dict[tuple[str, str], list[TrajectoryRecord]],
    order: list[tuple[str, str]],
    first: dict[tuple[str, str], int],
    key: tuple[str, str],
    position: int,
) -> None:
    if key not in entries:
        entries[key] = []
        order.append(key)
        first[key] = position


def _turn_groups(
    turn_key: tuple[str, str],
    records: Iterable[TrajectoryRecord],
    steps: dict[str, str | None],
    positions: Mapping[str, int],
) -> tuple[TrajectoryGroup, ...]:
    source_epoch, turn_id = turn_key
    values = tuple(sorted(records, key=lambda record: positions[record.record_id]))
    children = _step_groups(values, turn_key, steps)
    units: list[tuple[int, int, str | TrajectoryGroup]] = [
        (positions[record.record_id], 0, record.record_id)
        for record in values
        if steps.get(record.record_id, record.step_id) is None
    ]
    units.extend(
        (
            min(positions[record_id] for record_id in child.record_ids),
            1,
            child,
        )
        for child in children
    )
    units.sort(key=lambda item: item[:2])

    parts: list[tuple[tuple[str, ...], tuple[TrajectoryGroup, ...]]] = []
    direct: list[str] = []
    nested: list[TrajectoryGroup] = []
    for _position, _kind, unit in units:
        full = (isinstance(unit, str) and len(direct) >= TRAJECTORY_MAX_GROUP_RECORD_IDS) or (
            isinstance(unit, TrajectoryGroup) and len(nested) >= TRAJECTORY_MAX_GROUP_CHILDREN
        )
        if full:
            parts.append((tuple(direct), tuple(nested)))
            direct = []
            nested = []
        if isinstance(unit, str):
            direct.append(unit)
        else:
            nested.append(unit)
    if direct or nested:
        parts.append((tuple(direct), tuple(nested)))

    base_id = _bounded_group_id("turn", source_epoch, turn_id)
    label = f"Turn {turn_id}"
    return tuple(
        TrajectoryGroup(
            group_id=_part_group_id(base_id, "turn-part", index),
            kind=GroupKind.TURN,
            label=_part_label(label, index),
            record_ids=direct_ids,
            children=child_groups,
            turn_id=turn_id,
        )
        for index, (direct_ids, child_groups) in enumerate(parts)
    )


def _bounded_group_id(prefix: str, source_epoch: str, *parts: str) -> str:
    value = ":".join((prefix, source_epoch, *parts))
    if len(value.encode("utf-8")) <= TRAJECTORY_IDENTIFIER_MAX_BYTES:
        return value
    return f"{prefix}:{sha256(value.encode('utf-8')).hexdigest()}"


def _part_group_id(base_id: str, prefix: str, index: int) -> str:
    return base_id if index == 0 else _bounded_group_id(prefix, base_id, str(index + 1))


def _part_label(label: str, index: int) -> str:
    return label if index == 0 else f"{label} · continued {index + 1}"


def _between_groups(
    records: list[tuple[int, TrajectoryRecord]],
    turn_order: list[tuple[str, str]],
    turn_entries: dict[tuple[str, str], list[TrajectoryRecord]],
    turn_first: dict[tuple[str, str], int],
) -> list[tuple[float, TrajectoryGroup]]:
    if not records:
        return []
    if not turn_order:
        ordered = _order_between_records(records)
        return [
            (float(ordered[0][0]), group)
            for group in _between_groups_for(
                ordered,
                _between_group_id(("unpositioned", 0), turn_order),
            )
        ]
    intervals = [_turn_interval(turn_entries[key]) for key in turn_order]
    buckets: dict[tuple[str, int], list[tuple[int, TrajectoryRecord]]] = {}
    for position, record in records:
        slot = _timestamp_slot(record, intervals)
        bucket_key = ("unpositioned", 0) if slot is None else ("slot", slot)
        buckets.setdefault(bucket_key, []).append((position, record))
    result: list[tuple[float, TrajectoryGroup]] = []
    for bucket_key, bucket_values in buckets.items():
        values = _order_between_records(bucket_values)
        group_position = _between_position(bucket_key, values[0][0], turn_order, turn_first)
        result.extend(
            (group_position, group)
            for group in _between_groups_for(
                values,
                _between_group_id(bucket_key, turn_order),
            )
        )
    return result


def _between_groups_for(
    records: Iterable[tuple[int, TrajectoryRecord]], group_id: str
) -> tuple[TrajectoryGroup, ...]:
    values = tuple(record for _position, record in records)
    groups: list[TrajectoryGroup] = []
    for index, start in enumerate(range(0, len(values), TRAJECTORY_MAX_GROUP_RECORD_IDS)):
        chunk = values[start : start + TRAJECTORY_MAX_GROUP_RECORD_IDS]
        groups.append(
            TrajectoryGroup(
                group_id=_part_group_id(group_id, "between-turns-part", index),
                kind=GroupKind.BETWEEN_TURNS,
                label=_part_label("Between turns", index),
                record_ids=tuple(record.record_id for record in chunk),
            )
        )
    return tuple(groups)


def _order_between_records(
    records: Iterable[tuple[int, TrajectoryRecord]],
) -> list[tuple[int, TrajectoryRecord]]:
    values = list(records)
    if not values or not any(
        record.record_id.startswith(TRAJECTORY_THEATER_BUS_RECORD_PREFIX) for _, record in values
    ):
        return values
    return sorted(values, key=lambda item: _between_order_key(item[1], item[0]))


def _between_group_id(bucket: tuple[str, int], turn_order: list[tuple[str, str]]) -> str:
    if bucket[0] == "unpositioned":
        return "between-turns:unpositioned"
    slot = bucket[1]
    left = "start" if slot == 0 else _turn_boundary_token(turn_order[slot - 1])
    right = "end" if slot == len(turn_order) else _turn_boundary_token(turn_order[slot])
    return _bounded_group_id("between-turns", f"{left}|{right}")


def _turn_boundary_token(turn_key: tuple[str, str]) -> str:
    return f"{turn_key[0]}:{turn_key[1]}"


def _between_order_key(record: TrajectoryRecord, position: int) -> tuple[int, int | str, str, int]:
    if record.record_id.startswith(TRAJECTORY_THEATER_BUS_RECORD_PREFIX):
        return _bus_order_key(record, position)
    return (1, "", "", position)


def _bus_order_key(record: TrajectoryRecord, position: int) -> tuple[int, int | str, str, int]:
    value = record.record_id.removeprefix(TRAJECTORY_THEATER_BUS_RECORD_PREFIX)
    try:
        return (0, int(value), value, position)
    except ValueError:
        return (1, value, value, position)


def _turn_interval(records: Iterable[TrajectoryRecord]) -> tuple[float, float] | None:
    values = tuple(records)
    if not values or any(not _reliable_time(record) for record in values):
        return None
    starts: list[float] = []
    ends: list[float] = []
    for record in values:
        assert record.timing is not None
        if record.timing.start is None or record.timing.end is None:
            return None
        starts.append(record.timing.start)
        ends.append(record.timing.end)
    return min(starts), max(ends)


def _reliable_time(record: TrajectoryRecord) -> bool:
    return (
        record.timing is not None
        and record.timing.start is not None
        and record.timing.end is not None
        and record.timing.provenance in (TimingProvenance.SOURCE, TimingProvenance.OBSERVED)
    )


def _timestamp_slot(
    record: TrajectoryRecord, intervals: list[tuple[float, float] | None]
) -> int | None:
    if not _reliable_time(record) or not intervals:
        return None
    assert (
        record.timing is not None
        and record.timing.start is not None
        and record.timing.end is not None
    )
    start, end = record.timing.start, record.timing.end
    if intervals[0] is not None and end <= intervals[0][0]:
        return 0
    for index in range(len(intervals) - 1):
        left, right = intervals[index], intervals[index + 1]
        if left is not None and right is not None and left[1] <= start and end <= right[0]:
            return index + 1
    if intervals[-1] is not None and start >= intervals[-1][1]:
        return len(intervals)
    return None


def _between_position(
    bucket: tuple[str, int],
    fallback: int,
    turn_order: list[tuple[str, str]],
    first: dict[tuple[str, str], int],
) -> float:
    if bucket[0] != "slot" or not turn_order:
        return float(fallback)
    slot = bucket[1]
    if slot == 0:
        return float(first[turn_order[0]]) - 0.5
    if slot == len(turn_order):
        return float(first[turn_order[-1]]) + 0.5
    return (float(first[turn_order[slot - 1]]) + float(first[turn_order[slot]])) / 2


def _step_groups(
    records: Iterable[TrajectoryRecord], turn_key: tuple[str, str], steps: dict[str, str | None]
) -> tuple[TrajectoryGroup, ...]:
    entries: dict[str, list[TrajectoryRecord]] = {}
    order: list[str] = []
    for record in records:
        step_id = steps.get(record.record_id, record.step_id)
        if step_id is None:
            continue
        if step_id not in entries:
            entries[step_id] = []
            order.append(step_id)
        entries[step_id].append(record)
    source_epoch, turn_id = turn_key
    groups: list[TrajectoryGroup] = []
    for step_id in order:
        base_id = _bounded_group_id("step", source_epoch, turn_id, step_id)
        label = f"Step {step_id}"
        values = entries[step_id]
        for index, start in enumerate(range(0, len(values), TRAJECTORY_MAX_GROUP_RECORD_IDS)):
            chunk = values[start : start + TRAJECTORY_MAX_GROUP_RECORD_IDS]
            groups.append(
                TrajectoryGroup(
                    group_id=_part_group_id(base_id, "step-part", index),
                    kind=GroupKind.STEP,
                    label=_part_label(label, index),
                    record_ids=tuple(record.record_id for record in chunk),
                    step_id=step_id,
                    turn_id=turn_id,
                )
            )
    return tuple(groups)


__all__ = [
    "deduplicate_records",
    "deterministic_record_order",
    "group_records",
    "merge_records",
    "newer_record",
]
