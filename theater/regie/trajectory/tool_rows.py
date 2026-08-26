"""Immutable tool-operation rows for the Régie ledger."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from theater.regie.trajectory.constants import (
    TOOL_ROW_INPUT_COMPACT_FIELD_LIMIT,
    TOOL_ROW_INPUT_DETAIL_NAMES,
    TOOL_ROW_INPUT_FIELD_LIMIT,
    TOOL_ROW_INPUT_KEY_PRIORITY,
    TOOL_ROW_INPUT_VALUE_MAX_CHARS,
    TOOL_ROW_SUMMARY_MAX_CHARS,
)
from theater.regie.trajectory.render import format_duration, sanitize_text, status_label
from theater.trajectory import DetailField, TrajectoryRecord
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


def _normalized_name(value: str) -> str:
    return value.casefold().replace("-", "_").replace(" ", "_")


def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _input_field(operation: TrajectoryToolOperation) -> DetailField | None:
    for field in operation.call_details:
        if _normalized_name(field.name) in TOOL_ROW_INPUT_DETAIL_NAMES:
            return field
    return None


def _json_value(value: object) -> str:
    if isinstance(value, str):
        rendered = " ".join(_one_line(value).split())
    else:
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return _clip(rendered, TOOL_ROW_INPUT_VALUE_MAX_CHARS)


def _ordered_input_keys(value: dict[str, object]) -> tuple[str, ...]:
    preferred = [key for key in TOOL_ROW_INPUT_KEY_PRIORITY if key in value]
    return (*preferred, *(key for key in value if key not in preferred))


def _structured_input(value: object, *, field_limit: int) -> str:
    if not isinstance(value, dict):
        return _json_value(value)
    parts: list[str] = []
    for key in _ordered_input_keys(value):
        rendered = _json_value(value[key])
        if not rendered:
            continue
        parts.append(f"{_one_line(key)}={rendered}")
        if len(parts) >= field_limit:
            break
    return " · ".join(parts)


def _input_preview(operation: TrajectoryToolOperation, *, compact: bool) -> str | None:
    field = _input_field(operation)
    if field is None or not field.preview.text:
        return None
    field_limit = TOOL_ROW_INPUT_COMPACT_FIELD_LIMIT if compact else TOOL_ROW_INPUT_FIELD_LIMIT
    if field.preview.omitted_bytes == 0:
        try:
            return _structured_input(json.loads(field.preview.text), field_limit=field_limit)
        except (TypeError, ValueError):
            pass
    return _clip(" ".join(_one_line(field.preview.text).split()), TOOL_ROW_INPUT_VALUE_MAX_CHARS)


def tool_row_text(operation: TrajectoryToolOperation, *, compact: bool = False) -> ToolRowText:
    """Build bounded, plain values for one logical tool operation."""
    name = _one_line(operation.tool_name) if operation.tool_name else "unknown tool"
    source = _one_line(operation.source)
    preview = _input_preview(operation, compact=compact)
    if operation.identity is TrajectoryToolIdentity.CALL_ONLY:
        summary = f"{preview} · awaiting result" if preview else "awaiting result"
    elif operation.identity is TrajectoryToolIdentity.RESULT_ONLY:
        summary = "unmatched result"
    elif operation.identity is TrajectoryToolIdentity.UNKEYED_CALL:
        state = "unmatched call · awaiting result"
        summary = f"{preview} · {state}" if preview else state
    elif operation.identity is TrajectoryToolIdentity.UNKEYED_RESULT:
        summary = "unmatched result"
    else:
        summary = preview or "input unavailable"
    if operation.call_count > 1 or operation.result_count > 1:
        summary += f" · {operation.call_count + operation.result_count} records"
    if operation.records_truncated:
        summary += " · links clipped"
    summary = _clip(f"[{name}] {summary}", TOOL_ROW_SUMMARY_MAX_CHARS)
    return ToolRowText(
        event="⚙ TOOL",
        source=source,
        summary=summary,
        status=status_label(operation.status),
        duration=format_duration(operation.timing),
    )


__all__ = ["ToolIndex", "ToolRowText", "build_tool_index", "empty_tool_index", "tool_row_text"]
