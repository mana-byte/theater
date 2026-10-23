"""Shared scalar validation for daemon request adapters."""

from __future__ import annotations

import math
import numbers
from typing import Any

from theater.models import BadRequest


def integer_param(value: Any, key: str, *, method_name: str) -> int:
    """Return one strict integer request parameter."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise BadRequest(f"{method_name} parameter {key!r} must be an integer")
    if not -(2**63) <= value < 2**63:
        raise BadRequest(f"{method_name} parameter {key!r} is out of range")
    return value


def finite_number_param(value: Any, key: str, *, method_name: str) -> float:
    """Return one finite numeric request parameter, excluding booleans."""
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise BadRequest(f"{method_name} parameter {key!r} must be a finite number")
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise BadRequest(f"{method_name} parameter {key!r} must be a finite number") from None
    if not math.isfinite(result):
        raise BadRequest(f"{method_name} parameter {key!r} must be a finite number")
    return result
