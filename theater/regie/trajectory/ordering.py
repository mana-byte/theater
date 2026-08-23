"""Pure canonical traversal of bounded trajectory groups."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from theater.regie.trajectory.constants import TRAJECTORY_UI_RECORD_LIMIT
from theater.trajectory import TrajectoryGroup, TrajectoryRecord, group_records

GroupUnit = str | TrajectoryGroup


@dataclass(frozen=True, slots=True)
class TrajectoryOrdering:
    """One immutable ordering snapshot for a bounded record window."""

    source: tuple[TrajectoryRecord, ...]
    groups: tuple[TrajectoryGroup, ...]
    records: tuple[TrajectoryRecord, ...]
    _units: Mapping[int, tuple[GroupUnit, ...]]

    def group_units(self, group: TrajectoryGroup) -> tuple[GroupUnit, ...]:
        return self._units.get(id(group), ())


def complete_groups(
    records: Sequence[TrajectoryRecord], groups: Sequence[TrajectoryGroup]
) -> tuple[TrajectoryGroup, ...]:
    """Use supplied groups only when they cover every loaded record."""
    if not groups:
        return group_records(records)
    known: set[str] = set()

    def visit(group: TrajectoryGroup) -> None:
        known.update(group.record_ids)
        for child in group.children:
            visit(child)

    for group in groups:
        visit(group)
    if {record.record_id for record in records} <= known:
        return tuple(groups)
    return group_records(records)


def build_ordering(
    records: Sequence[TrajectoryRecord], groups: Sequence[TrajectoryGroup]
) -> TrajectoryOrdering:
    """Build one canonical chronology without rescanning child subtrees."""
    source = tuple(records[:TRAJECTORY_UI_RECORD_LIMIT])
    grouped = complete_groups(source, groups)
    positions = {record.record_id: index for index, record in enumerate(source)}
    units_by_id: dict[int, tuple[GroupUnit, ...]] = {}
    earliest_by_id: dict[int, int | None] = {}

    def visit(group: TrajectoryGroup) -> int | None:
        group_id = id(group)
        if group_id in earliest_by_id:
            return earliest_by_id[group_id]
        units: list[tuple[int, int, GroupUnit]] = []
        for record_id in group.record_ids:
            if (position := positions.get(record_id)) is not None:
                units.append((position, 0, record_id))
        for child in group.children:
            if (position := visit(child)) is not None:
                units.append((position, 1, child))
        units.sort(key=lambda item: item[:2])
        units_by_id[group_id] = tuple(unit for _position, _kind, unit in units)
        earliest = min((position for position, _kind, _unit in units), default=None)
        earliest_by_id[group_id] = earliest
        return earliest

    for group in grouped:
        visit(group)
    by_id = {record.record_id: record for record in source}
    ordered: list[TrajectoryRecord] = []
    seen: set[str] = set()

    def emit(group: TrajectoryGroup) -> None:
        for unit in units_by_id[id(group)]:
            if isinstance(unit, str):
                if unit not in seen and (record := by_id.get(unit)) is not None:
                    seen.add(unit)
                    ordered.append(record)
            else:
                emit(unit)

    for group in grouped:
        emit(group)
    ordered.extend(record for record in source if record.record_id not in seen)
    return TrajectoryOrdering(
        source,
        grouped,
        tuple(ordered),
        MappingProxyType(units_by_id),
    )


def canonical_group_records(
    records: Sequence[TrajectoryRecord], groups: Sequence[TrajectoryGroup]
) -> tuple[TrajectoryRecord, ...]:
    """Flatten complete groups in the same chronology used by ledger rows."""
    return build_ordering(records, groups).records


def group_units(
    records: Sequence[TrajectoryRecord], group: TrajectoryGroup
) -> tuple[GroupUnit, ...]:
    """Order direct records and child groups by their earliest source position."""
    return build_ordering(records, (group,)).group_units(group)


__all__ = [
    "TrajectoryOrdering",
    "build_ordering",
    "canonical_group_records",
    "complete_groups",
    "group_units",
]
