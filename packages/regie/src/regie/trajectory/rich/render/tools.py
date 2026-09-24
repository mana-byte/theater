"""Immutable tool-operation rows for the Régie ledger."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from regie.trajectory.domain import TrajectoryRecord
from regie.trajectory.domain.grouping import deterministic_record_order
from regie.trajectory.domain.tools import (
    TrajectoryToolOperation,
    tool_operations_for_records,
)


@dataclass(frozen=True, slots=True)
class ToolIndex:
    """A canonical operation projection and its stable display anchors."""

    ordered: tuple[TrajectoryToolOperation, ...] = ()
    by_id: Mapping[str, TrajectoryToolOperation] = MappingProxyType({})
    by_record_id: Mapping[str, str] = MappingProxyType({})
    anchor_by_id: Mapping[str, str] = MappingProxyType({})
    members_by_id: Mapping[str, tuple[str, ...]] = MappingProxyType({})


def empty_tool_index() -> ToolIndex:
    return ToolIndex()


def build_tool_index(records: Iterable[TrajectoryRecord]) -> ToolIndex:
    """Project tool operations once and map every member to one display row."""
    ordered_records = deterministic_record_order(records)
    positions = {record.record_id: index for index, record in enumerate(ordered_records)}
    operations = tool_operations_for_records(ordered_records)
    by_id: dict[str, TrajectoryToolOperation] = {}
    by_record_id: dict[str, str] = {}
    anchors: dict[str, str] = {}
    members_by_id: dict[str, tuple[str, ...]] = {}
    for operation in operations:
        previous = by_id.setdefault(operation.operation_id, operation)
        if previous != operation:
            raise ValueError("trajectory tool projection repeated a canonical operation ID")
        member_ids = tuple(
            sorted(
                {*operation.call_record_ids, *operation.result_record_ids},
                key=lambda record_id: positions.get(record_id, len(positions)),
            )
        )
        if not member_ids:
            raise ValueError("trajectory tool operation has no canonical members")
        if any(record_id not in positions for record_id in member_ids):
            raise ValueError("trajectory tool operation references a missing record")
        previous_members = members_by_id.setdefault(operation.operation_id, member_ids)
        if previous_members != member_ids:
            raise ValueError("trajectory tool projection changed canonical operation membership")
        anchors[operation.operation_id] = member_ids[0]
        for record_id in member_ids:
            prior = by_record_id.setdefault(record_id, operation.operation_id)
            if prior != operation.operation_id:
                raise ValueError(
                    "trajectory tool projection joined a record to multiple operations"
                )
    return ToolIndex(
        ordered=operations,
        by_id=MappingProxyType(by_id),
        by_record_id=MappingProxyType(by_record_id),
        anchor_by_id=MappingProxyType(anchors),
        members_by_id=MappingProxyType(members_by_id),
    )


__all__ = ["ToolIndex", "build_tool_index", "empty_tool_index"]
