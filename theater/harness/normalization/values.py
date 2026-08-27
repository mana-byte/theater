"""Shared bounded trajectory value conversion."""

from __future__ import annotations

import json
import math

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


def trajectory_identifier(value: object) -> str | None:
    """Return a bounded, control-free trajectory identifier."""
    if not isinstance(value, str) or not value:
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(encoded) > TRAJECTORY_IDENTIFIER_MAX_BYTES:
        return None
    if any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in value):
        return None
    return value


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
