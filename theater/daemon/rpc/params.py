"""Shared RPC parameter validation helpers.

Required-parameter extraction, string/optional-string parsing, worktree
parameter validation, and response-format serialization.  Used by every
handler module that needs to pull typed values out of the raw ``params`` dict.
"""

from __future__ import annotations

import json
import math
import numbers
from typing import Any

from theater.harness import HARNESSES, normalize
from theater.models import BadRequest

_JSON_REPLY_INSTRUCTION = (
    "Return your final answer as a single bare JSON value (no code fences, no prose) "
    "matching this schema hint: {schema}"
)


def _require(params: dict, key: str) -> Any:
    if key not in params or params[key] in (None, ""):
        raise BadRequest(f"missing required parameter {key!r}")
    return params[key]


def _string_param(params: dict, key: str, *, method_name: str, allow_empty: bool = False) -> str:
    if key not in params or params[key] is None:
        raise BadRequest(f"{method_name} requires string parameter {key!r}")
    value = params[key]
    if not isinstance(value, str):
        raise BadRequest(f"{method_name} parameter {key!r} must be a string")
    if not allow_empty and value == "":
        raise BadRequest(f"{method_name} parameter {key!r} must be a non-empty string")
    return value


def _optional_string_param(params: dict, key: str, *, method_name: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise BadRequest(f"{method_name} parameter {key!r} must be a string or null")
    return value


def _integer_param(value: Any, key: str, *, method_name: str) -> int:
    """Return one strict integer RPC parameter."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise BadRequest(f"{method_name} parameter {key!r} must be an integer")
    if not -(2**63) <= value < 2**63:
        raise BadRequest(f"{method_name} parameter {key!r} is out of range")
    return value


def _finite_number_param(value: Any, key: str, *, method_name: str) -> float:
    """Return one finite numeric RPC parameter, excluding booleans."""
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise BadRequest(f"{method_name} parameter {key!r} must be a finite number")
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise BadRequest(f"{method_name} parameter {key!r} must be a finite number") from None
    if not math.isfinite(result):
        raise BadRequest(f"{method_name} parameter {key!r} must be a finite number")
    return result


def _validate_worktree_param(value: Any) -> str | bool | None:
    """Normalise and validate the ``worktree`` RPC parameter.

    Accepts ``True``, ``False``, ``None``, or a non-empty string. Rejects
    integers, lists, dicts, and empty strings so that truthiness never
    turns an unexpected type into a unique worktree.
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


def _serialized_response_format(params: dict) -> str | None:
    raw = params.get("response_format")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise BadRequest(
            "response_format must be a JSON object or null; pass a schema hint "
            "object such as {'type': 'object'}"
        )
    return json.dumps(raw, sort_keys=True, separators=(",", ":"))


def _prompt_with_response_format(prompt: str, response_format: str | None) -> str:
    if response_format is None:
        return prompt
    return f"{_JSON_REPLY_INSTRUCTION.format(schema=response_format)}\n\n{prompt}"


def _reject_response_format_resume(
    harness_name: Any, resume: Any, response_format: str | None
) -> None:
    if response_format is None or not resume or not isinstance(harness_name, str):
        return
    harness = HARNESSES.get(normalize(harness_name))
    if harness is not None and not harness.resume_takes_prompt:
        raise BadRequest(
            f"harness {harness_name!r} cannot resume a session with response_format; "
            f"resume it without one and use send to deliver the task"
        )
