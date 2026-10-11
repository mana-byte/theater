"""Decode JSON values that can safely cross Theater's wire formats."""

from __future__ import annotations

import json
import math


def decode_finite_json(raw: str | None) -> tuple[bool, object]:
    """Return a JSON value, rejecting non-finite numbers at every depth."""
    if raw is None:
        return False, None
    try:
        value = json.loads(raw, parse_constant=_reject_nonfinite)
        if not finite_json_value(value):
            return False, None
    except (TypeError, ValueError, RecursionError):
        return False, None
    return True, value


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r}")


def finite_json_value(value: object) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(finite_json_value(item) for item in value.values())
    if isinstance(value, list):
        return all(finite_json_value(item) for item in value)
    return True


__all__ = ["decode_finite_json", "finite_json_value"]
