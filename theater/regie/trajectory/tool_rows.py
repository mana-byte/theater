"""Immutable tool-operation rows for the Régie ledger."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from theater.regie.trajectory.constants import TOOL_ROW_SUMMARY_MAX_CHARS
from theater.regie.trajectory.render import format_duration, sanitize_text, status_label
from theater.trajectory import TrajectoryRecord
from theater.trajectory.grouping import deterministic_record_order
from theater.trajectory.tools import (
    TrajectoryToolIdentity,
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


@dataclass(frozen=True, slots=True)
class ToolRowText:
    event: str
    source: str
    summary: str
    status: str
    duration: str


def _one_line(value: str) -> str:
    return sanitize_text(value).replace("\r", " ").replace("\n", " ").replace("\t", " ")


def _preview(operation: TrajectoryToolOperation) -> str | None:
    aliases = {"output", "response", "result", "tool_result", "error"}
    for field in operation.result_details:
        if field.name.casefold().replace("-", "_").replace(" ", "_") in aliases:
            return _one_line(field.preview.text)
    return None


def tool_row_text(operation: TrajectoryToolOperation, *, compact: bool = False) -> ToolRowText:
    """Build bounded, plain values for one logical tool operation."""
    name = _one_line(operation.tool_name) if operation.tool_name else "unknown tool"
    source = _one_line(operation.source)
    if operation.identity is TrajectoryToolIdentity.CALL_ONLY:
        summary = "awaiting result"
    elif operation.identity is TrajectoryToolIdentity.RESULT_ONLY:
        summary = "unmatched result"
    elif operation.identity is TrajectoryToolIdentity.UNKEYED_CALL:
        summary = "unmatched call · awaiting result"
    elif operation.identity is TrajectoryToolIdentity.UNKEYED_RESULT:
        summary = "unmatched result"
    else:
        summary = _preview(operation) or "result unavailable"
    if operation.call_count > 1 or operation.result_count > 1:
        summary += f" · {operation.call_count + operation.result_count} records"
    if operation.records_truncated:
        summary += " · links clipped"
    summary = f"[{name}] {summary}"[:TOOL_ROW_SUMMARY_MAX_CHARS]
    return ToolRowText(
        event="⚙ TOOL",
        source=source,
        summary=summary,
        status=status_label(operation.status),
        duration=format_duration(operation.timing),
    )


__all__ = ["ToolIndex", "ToolRowText", "build_tool_index", "empty_tool_index", "tool_row_text"]
