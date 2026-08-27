"""Shared bounded trajectory value conversion."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from theater.constants.trajectory import TRAJECTORY_IDENTIFIER_MAX_BYTES
from theater.trajectory.content import ContentFormat, DetailField
from theater.trajectory.enums import TrajectoryStatus


def safe_trajectory_text(value: object) -> str:
    """Return UTF-8-safe trajectory text."""
    if not isinstance(value, str):
        return ""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return value.encode("utf-8", "replace").decode("utf-8")
    return value


def trajectory_identifier(value: object, *, overflow_prefix: str | None = None) -> str | None:
    """Return a bounded, control-free trajectory identifier."""
    if not isinstance(value, str) or not value:
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in value):
        return None
    if len(encoded) <= TRAJECTORY_IDENTIFIER_MAX_BYTES:
        return value
    if overflow_prefix is None:
        return None
    return f"{overflow_prefix}:{hashlib.sha256(encoded).hexdigest()}"


def stable_json(value: object) -> str:
    """Serialize a value deterministically for trajectory detail text."""
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        )
    except (TypeError, ValueError, UnicodeError):
        return json.dumps(str(value), ensure_ascii=True)


def trajectory_detail(name: str, value: object, *, format: ContentFormat) -> DetailField:
    """Build a trajectory detail field from text or stable JSON."""
    text = value if isinstance(value, str) else stable_json(value)
    return DetailField.from_text(name, safe_trajectory_text(text), format=format)


def nonnegative_int(value: object) -> int:
    """Coerce a finite nonnegative integer value, else zero."""
    if type(value) is int and value >= 0:
        return value
    if type(value) is float and math.isfinite(value) and value >= 0 and value.is_integer():
        return int(value)
    return 0


def finite_float(value: object) -> float | None:
    """Coerce a finite numeric value, excluding booleans."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def trajectory_status(value: object, default: TrajectoryStatus) -> TrajectoryStatus:
    """Normalize shared trajectory status aliases."""
    if isinstance(value, TrajectoryStatus):
        return value
    if not isinstance(value, str):
        return default
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
    return aliases.get(value.lower().replace("-", "_"), default)


def content_blocks_text(value: object) -> str:
    """Flatten Claude/Codex content-block lists into trajectory text."""
    if isinstance(value, str):
        return safe_trajectory_text(value)
    if isinstance(value, list):
        text = "".join(
            safe_trajectory_text(item.get("text"))
            for item in value
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
        if text:
            return text
    return safe_trajectory_text(stable_json(value)) if value is not None else ""


def loose_trajectory_text(value: object) -> str:
    """Like safe_trajectory_text but also JSON-dumps dict/list/tuple."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return value.encode("utf-8", "replace").decode("utf-8")
        return value
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, default=str, sort_keys=True)
        except (TypeError, ValueError):
            return ""
    return ""


def optional_trajectory_detail(
    name: str, value: object, *, format: ContentFormat = ContentFormat.TEXT
) -> DetailField | None:
    """Like trajectory_detail but returns None on empty/invalid and swallows ValueError."""
    if value is None:
        return None
    text = loose_trajectory_text(value)
    if not text and isinstance(value, (int, float, bool)):
        text = json.dumps(value)
    if not text:
        return None
    try:
        return DetailField.from_text(name, text, format=format)
    except ValueError:
        return None


def decode_json_record(line: object) -> dict | None:
    """Strip, json.loads, and return a dict or None."""
    if isinstance(line, (bytes, bytearray)):
        line = line.decode("utf-8", errors="replace")
    if not isinstance(line, str):
        return None
    line = line.strip()
    if not line:
        return None
    try:
        record = json.loads(line)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def first_key(mapping: dict, *keys: str, coerce: Any = None) -> object:
    """Return the first present alias key, optionally coerced."""
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return coerce(mapping[key]) if coerce is not None else mapping[key]
    return None


def first_key_of(mappings: tuple[dict, ...], keys: tuple[str, ...], coerce: Any = None) -> object:
    """Return the first present alias key across multiple mappings."""
    for mapping in mappings:
        result = first_key(mapping, *keys, coerce=coerce)
        if result is not None:
            return result
    return None


def revision_from(*mappings: dict) -> int:
    """Extract a revision or version integer from the first mapping that has one."""
    for mapping in mappings:
        for key in ("revision", "version"):
            candidate = nonnegative_int(mapping.get(key))
            if candidate or mapping.get(key) in (0, 0.0):
                return candidate
    return 0
