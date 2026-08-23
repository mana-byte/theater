"""Pure canonical traversal of bounded trajectory groups."""

from __future__ import annotations

from collections.abc import Sequence

from theater.regie.trajectory.constants import TRAJECTORY_UI_RECORD_LIMIT
from theater.trajectory import TrajectoryGroup, TrajectoryRecord, group_records


def _all_record_ids(group: TrajectoryGroup) -> set[str]:
    record_ids = set(group.record_ids)
    for child in group.children:
        record_ids.update(_all_record_ids(child))
    return record_ids


def complete_groups(
    records: Sequence[TrajectoryRecord], groups: Sequence[TrajectoryGroup]
) -> tuple[TrajectoryGroup, ...]:
    """Use supplied groups only when they cover every loaded record."""
    if not groups:
        return group_records(records)
    known = {record_id for group in groups for record_id in _all_record_ids(group)}
    if {record.record_id for record in records} <= known:
        return tuple(groups)
    return group_records(records)


def group_units(
    records: Sequence[TrajectoryRecord], group: TrajectoryGroup
) -> tuple[str | TrajectoryGroup, ...]:
    """Order direct records and child groups by their earliest source position."""
    positions = {record.record_id: index for index, record in enumerate(records)}
    units: list[tuple[int, int, str | TrajectoryGroup]] = []
    for record_id in group.record_ids:
        if record_id in positions:
            units.append((positions[record_id], 0, record_id))
    for child in group.children:
        child_positions = [
            positions[record_id] for record_id in _all_record_ids(child) if record_id in positions
        ]
        if child_positions:
            units.append((min(child_positions), 1, child))
    return tuple(unit for _position, _kind, unit in sorted(units, key=lambda item: item[:2]))


def canonical_group_records(
    records: Sequence[TrajectoryRecord], groups: Sequence[TrajectoryGroup]
) -> tuple[TrajectoryRecord, ...]:
    """Flatten complete groups in the same chronology used by ledger rows."""
    source = tuple(records[:TRAJECTORY_UI_RECORD_LIMIT])
    grouped = complete_groups(source, groups)
    by_id = {record.record_id: record for record in source}
    ordered: list[TrajectoryRecord] = []
    seen: set[str] = set()

    def visit(group: TrajectoryGroup) -> None:
        for unit in group_units(source, group):
            if isinstance(unit, str):
                if unit not in seen and (record := by_id.get(unit)) is not None:
                    seen.add(unit)
                    ordered.append(record)
            else:
                visit(unit)

    for group in grouped:
        visit(group)
    ordered.extend(record for record in source if record.record_id not in seen)
    return tuple(ordered)


__all__ = ["canonical_group_records", "complete_groups", "group_units"]
