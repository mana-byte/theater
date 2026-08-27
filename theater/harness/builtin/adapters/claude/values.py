"""Claude native value conversion helpers."""

from __future__ import annotations

import json
import math

from theater.constants.trajectory import TRAJECTORY_IDENTIFIER_MAX_BYTES
from theater.trajectory.content import ContentFormat, DetailField
from theater.trajectory.enums import TrajectoryStatus


def _safe_trajectory_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return value.encode("utf-8", "replace").decode("utf-8")
    return value


def _trajectory_id(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in value):
        return None
    if len(value.encode("utf-8")) > TRAJECTORY_IDENTIFIER_MAX_BYTES:
        return None
    return value


def _claude_mcp_identity(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str) or not value.startswith("mcp__"):
        return None
    server, separator, tool = value.removeprefix("mcp__").partition("__")
    if not separator:
        return None
    server_id = _trajectory_id(server)
    tool_id = _trajectory_id(tool)
    return (server_id, tool_id) if server_id is not None and tool_id is not None else None


def _stable_json(value: object) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        )
    except (TypeError, ValueError, UnicodeError):
        return json.dumps(str(value), ensure_ascii=True)


def _trajectory_detail(name: str, value: object, *, format: ContentFormat) -> DetailField:
    text = value if isinstance(value, str) else _stable_json(value)
    return DetailField.from_text(name, _safe_trajectory_text(text), format=format)


def _trajectory_int(value: object) -> int:
    if type(value) is int and value >= 0:
        return value
    if type(value) is float and math.isfinite(value) and value >= 0 and value.is_integer():
        return int(value)
    return 0


def _trajectory_float(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _trajectory_status(value: object, default: TrajectoryStatus) -> TrajectoryStatus:
    if isinstance(value, TrajectoryStatus):
        return value
    if not isinstance(value, str):
        return default
    normalized = value.lower().replace("-", "_")
    aliases = {
        "complete": TrajectoryStatus.COMPLETED,
        "completed": TrajectoryStatus.COMPLETED,
        "done": TrajectoryStatus.COMPLETED,
        "success": TrajectoryStatus.COMPLETED,
        "failed": TrajectoryStatus.ERROR,
        "failure": TrajectoryStatus.ERROR,
        "error": TrajectoryStatus.ERROR,
        "cancelled": TrajectoryStatus.CANCELLED,
        "canceled": TrajectoryStatus.CANCELLED,
        "aborted": TrajectoryStatus.INTERRUPTED,
        "in_progress": TrajectoryStatus.RUNNING,
        "running": TrajectoryStatus.RUNNING,
        "partial": TrajectoryStatus.PARTIAL,
        "interrupted": TrajectoryStatus.INTERRUPTED,
        "pending": TrajectoryStatus.PENDING,
    }
    return aliases.get(normalized, default)


def _claude_revision(record: dict) -> int:
    message = record.get("message")
    values = [record, message] if isinstance(message, dict) else [record]
    for value in values:
        for key in ("revision", "version"):
            candidate = _trajectory_int(value.get(key))
            if candidate or value.get(key) in (0, 0.0):
                return candidate
    return 0


def _claude_block_native_id(
    block: dict, base_id: str | None, record_id: str | None, ordinal: int
) -> str | None:
    explicit = _trajectory_id(block.get("id"))
    if explicit is not None:
        return explicit
    if record_id is not None:
        return record_id if ordinal == 0 else f"{record_id}:block:{ordinal}"
    if base_id is not None:
        return base_id if ordinal == 0 else f"{base_id}:block:{ordinal}"
    return None


def _claude_content_text(value: object) -> str:
    if isinstance(value, str):
        return _safe_trajectory_text(value)
    if isinstance(value, list):
        text = "".join(
            _safe_trajectory_text(item.get("text"))
            for item in value
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
        if text:
            return text
    return _safe_trajectory_text(_stable_json(value)) if value is not None else ""


def _relativise(path: str, cwd: str | None) -> str | None:
    if not path:
        return None
    if not path.startswith("/"):
        return path
    if cwd is None:
        return None
    c = cwd.rstrip("/") + "/"
    if not (path == cwd or path.startswith(c)):
        return None
    return "." if path == cwd else path[len(c) :]
