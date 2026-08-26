"""Conservative structured file-activity projection."""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass

from theater.constants.regie_trajectory import (
    TRAJECTORY_FILE_PATH_KEYS,
    TRAJECTORY_INSIGHT_ROW_LIMIT,
    TRAJECTORY_TOOL_READ_HINTS,
    TRAJECTORY_TOOL_WRITE_HINTS,
)
from theater.regie.trajectory.analysis.models import FileActivity, FileOperationActivity
from theater.regie.trajectory.analysis.waterfall import operation_position
from theater.regie.trajectory.render.tools import ToolIndex
from theater.trajectory import ContentFormat, TrajectoryStatus, TrajectoryToolOperation


@dataclass(slots=True)
class _FileAccumulator:
    modes: set[str]
    operations: list[tuple[int, FileOperationActivity]]
    records: list[str]
    record_seen: set[str]
    status: TrajectoryStatus
    position: int = -1


def _field_key(value: str) -> str:
    return value.casefold().replace("-", "_").replace(".", "_").replace(" ", "_")


def _tool_mode(tool_name: str | None) -> str:
    value = (tool_name or "").casefold()
    parts = frozenset(part for part in re.split(r"[^a-z0-9]+", value) if part)
    if any(hint in parts for hint in TRAJECTORY_TOOL_WRITE_HINTS):
        return "write"
    if any(hint in parts for hint in TRAJECTORY_TOOL_READ_HINTS):
        return "read"
    return "reference"


def _append_path(result: list[tuple[str, str]], path: str, mode: str) -> None:
    value = path.strip()
    if not value or "\n" in value or "\r" in value or len(value) > 2048:
        return
    item = (value, mode)
    if item not in result:
        result.append(item)


def _append_path_values(result: list[tuple[str, str]], value: object, mode: str) -> None:
    if isinstance(value, str):
        _append_path(result, value, mode)
    elif isinstance(value, (list, tuple)):
        for candidate in value[:64]:
            if isinstance(candidate, str):
                _append_path(result, candidate, mode)


def _paths_from_value(
    value: object,
    result: list[tuple[str, str]],
    *,
    mode: str,
    depth: int = 0,
) -> None:
    if depth > 3:
        return
    if isinstance(value, Mapping):
        for key, candidate in value.items():
            normalized = _field_key(str(key))
            if normalized in TRAJECTORY_FILE_PATH_KEYS:
                _append_path_values(result, candidate, mode)
            elif isinstance(candidate, (Mapping, list, tuple)):
                _paths_from_value(candidate, result, mode=mode, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        for candidate in value[:64]:
            if isinstance(candidate, (Mapping, list, tuple)):
                _paths_from_value(candidate, result, mode=mode, depth=depth + 1)


def _operation_paths(operation: TrajectoryToolOperation) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    for field in operation.call_details:
        key = _field_key(field.name)
        if field.format is ContentFormat.PATH:
            mode = key.rpartition("_")[2] if key.endswith(("_read", "_write")) else "reference"
            _append_path(result, field.preview.text, mode)
            continue
        if key not in {"args", "arguments", "input", "parameters", "tool_input"}:
            continue
        try:
            value = json.loads(field.preview.text)
        except (TypeError, ValueError):
            continue
        _paths_from_value(value, result, mode=_tool_mode(operation.tool_name))
    return tuple(result)


def build_file_activity(tools: ToolIndex, positions: Mapping[str, int]) -> tuple[FileActivity, ...]:
    collected: OrderedDict[str, _FileAccumulator] = OrderedDict()
    for operation in tools.ordered:
        members = (*operation.call_record_ids, *operation.result_record_ids)
        if not members:
            continue
        anchor = operation.call_record_ids[0] if operation.call_record_ids else members[0]
        paths: OrderedDict[str, set[str]] = OrderedDict()
        for path, mode in _operation_paths(operation):
            paths.setdefault(path, set()).add(mode)
        position = operation_position(operation, positions)
        for path, modes in paths.items():
            item = collected.setdefault(
                path,
                _FileAccumulator(
                    modes=set(),
                    operations=[],
                    records=[],
                    record_seen=set(),
                    status=operation.status,
                ),
            )
            item.modes.update(modes)
            item.operations.append(
                (
                    position,
                    FileOperationActivity(
                        operation_id=operation.operation_id,
                        record_id=anchor,
                        record_ids=members,
                        modes=frozenset(modes),
                        tool_name=operation.tool_name,
                        status=operation.status,
                        timing=operation.timing,
                    ),
                )
            )
            for record_id in members:
                if record_id not in item.record_seen:
                    item.record_seen.add(record_id)
                    item.records.append(record_id)
            if position >= item.position:
                item.status = operation.status
                item.position = position
    rows = [
        FileActivity(
            path=path,
            modes=frozenset(item.modes),
            record_ids=tuple(item.records),
            status=item.status,
            operations=tuple(
                operation for _, operation in sorted(item.operations, key=lambda item: item[0])
            ),
        )
        for path, item in collected.items()
    ]
    rows.sort(key=lambda row: row.path.casefold())
    return tuple(rows[:TRAJECTORY_INSIGHT_ROW_LIMIT])


__all__ = ["build_file_activity"]
