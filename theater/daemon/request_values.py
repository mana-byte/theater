"""Shared value validation for daemon request adapters."""

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


def require(params: dict, key: str) -> Any:
    if key not in params or params[key] in (None, ""):
        raise BadRequest(f"missing required parameter {key!r}")
    return params[key]


def string_param(params: dict, key: str, *, method_name: str, allow_empty: bool = False) -> str:
    if key not in params or params[key] is None:
        raise BadRequest(f"{method_name} requires string parameter {key!r}")
    value = params[key]
    if not isinstance(value, str):
        raise BadRequest(f"{method_name} parameter {key!r} must be a string")
    if not allow_empty and value == "":
        raise BadRequest(f"{method_name} parameter {key!r} must be a non-empty string")
    return value


def optional_string_param(params: dict, key: str, *, method_name: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise BadRequest(f"{method_name} parameter {key!r} must be a string or null")
    return value


def validate_worktree_param(value: Any) -> str | bool | None:
    """Normalise and validate the ``worktree`` RPC parameter.

    Only bool, None, or a non-empty string, so truthiness never turns an unexpected type
    into a unique worktree.
    """
    if value is None or value is False:
        return False
    if value is True:
        return True
    if isinstance(value, str):
        if not value.strip():
            raise BadRequest(
                "worktree name must be a non-empty string; an empty string is "
                "not a valid named-worktree name"
            )
        return value
    raise BadRequest(f"worktree parameter must be bool, str, or None; got {type(value).__name__}")
