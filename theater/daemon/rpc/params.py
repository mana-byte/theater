"""RPC adapters and compatibility names for shared request values."""

from __future__ import annotations

from theater.daemon import request_values
from theater.daemon.skill_cards import compose_prompt

_require = request_values.require
_string_param = request_values.string_param
_optional_string_param = request_values.optional_string_param
_validate_worktree_param = request_values.validate_worktree_param


def _prompt_with_response_format(prompt: str, response_format: str | None) -> str:
    return compose_prompt(prompt, response_format=response_format)
