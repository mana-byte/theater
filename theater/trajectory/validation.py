"""Strict helpers shared by trajectory wire decoders."""

from __future__ import annotations

import math
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from theater.trajectory.enums import TrajectoryValidationError


def enum_value(enum_type: type[StrEnum], value: object, label: str) -> Any:
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        raise TrajectoryValidationError(f"{label} must be a valid {enum_type.__name__} value")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise TrajectoryValidationError(f"{label} has unknown value {value!r}") from exc


def mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise TrajectoryValidationError(f"{label} must be an object with string keys")
    return value


def keys(
    value: Mapping[str, object], *, required: set[str], optional: set[str], label: str
) -> None:
    present = set(value)
    missing = required - present
    unknown = present - required - optional
    if missing:
        raise TrajectoryValidationError(f"{label} is missing keys: {', '.join(sorted(missing))}")
    if unknown:
        raise TrajectoryValidationError(f"{label} has unknown keys: {', '.join(sorted(unknown))}")


def string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TrajectoryValidationError(f"{label} must be a string")
    return value


def string_or_none(value: object, label: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise TrajectoryValidationError(f"{label} must be a string or null")
    return value


def integer(value: object, label: str) -> int:
    if type(value) is not int:
        raise TrajectoryValidationError(f"{label} must be an integer")
    return value


def number_or_none(value: object, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TrajectoryValidationError(f"{label} must be a finite number or null")
    return float(value)


def boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise TrajectoryValidationError(f"{label} must be a boolean")
    return value


def sequence(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise TrajectoryValidationError(f"{label} must be an array")
    return value
