"""OpenTelemetry-safe scalar attribute helpers."""

from __future__ import annotations

import math

_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
Scalar = str | int | float | bool


def optional(attributes: dict[str, Scalar], key: str, value: object) -> None:
    """Add a finite scalar value and omit every absent or unsupported value."""
    scalar = scalar_value(value)
    if scalar is not None:
        attributes[key] = scalar


def scalar_value(value: object) -> Scalar | None:
    """Return one OpenTelemetry-safe scalar or None."""
    if isinstance(value, str) or type(value) is bool:
        return value
    if type(value) is int and _INT64_MIN <= value <= _INT64_MAX:
        return value
    return value if isinstance(value, float) and math.isfinite(value) else None


__all__ = ["Scalar", "optional", "scalar_value"]
