"""RPC adapters and compatibility names for shared request values."""

from __future__ import annotations

from theater.daemon import request_values
from theater.daemon.response_contracts import resolve_response_format
from theater.daemon.response_contracts.prompts import HINT_INSTRUCTION
from theater.daemon.response_contracts.registry import reject_response_format_resume
from theater.daemon.skill_cards import compose_prompt

#: Kept for the ``daemon.methods`` facade; the text lives with the other contract prompts.
_JSON_REPLY_INSTRUCTION = HINT_INSTRUCTION

_finite_number_param = request_values.finite_number_param
_integer_param = request_values.integer_param
_require = request_values.require
_string_param = request_values.string_param
_optional_string_param = request_values.optional_string_param
_validate_worktree_param = request_values.validate_worktree_param
_reject_response_format_resume = reject_response_format_resume


def _serialized_response_format(daemon, params: dict, *, creator_id: str | None) -> str | None:
    """The stored form of ``response_format``; custom strict names belong to ``creator_id``."""
    return resolve_response_format(
        daemon.store, params.get("response_format"), creator_id=creator_id
    )


def _prompt_with_response_format(prompt: str, response_format: str | None) -> str:
    return compose_prompt(prompt, response_format=response_format)
