"""Shared RPC parameter validation helpers for every handler module."""

from __future__ import annotations

import math
import numbers
from typing import Any

from theater.daemon.response_contracts import resolve_response_format
from theater.daemon.response_contracts.prompts import HINT_INSTRUCTION
from theater.daemon.skill_cards import compose_prompt
from theater.harness import HARNESSES, normalize
from theater.models import BadRequest

#: Kept for the ``daemon.methods`` facade; the text lives with the other contract prompts.
_JSON_REPLY_INSTRUCTION = HINT_INSTRUCTION


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


def _serialized_response_format(daemon, params: dict, *, creator_id: str | None) -> str | None:
    """The stored form of ``response_format``; custom strict names belong to ``creator_id``."""
    return resolve_response_format(
        daemon.store, params.get("response_format"), creator_id=creator_id
    )


def _prompt_with_response_format(prompt: str, response_format: str | None) -> str:
    return compose_prompt(prompt, response_format=response_format)


def _prompt_with_interruption_card(
    prompt: str, *, interrupted: str | None, by: str, running: list[tuple[str, str, str]]
) -> str:
    """Tell a replaced child what survived the interrupt so it awaits instead of respawning."""
    turn = f"your turn {interrupted}" if interrupted else "your turn"
    card = f"[Theater: {turn} was interrupted by {by}."
    if running:
        listed = ", ".join(f"{handle} -> {name} ({state})" for handle, name, state in running)
        card += (
            f" Jobs you launched that are still running: {listed}. "
            "They are still running: await them, do not respawn."
        )
    return f"{card}]\n\n{prompt}"


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
