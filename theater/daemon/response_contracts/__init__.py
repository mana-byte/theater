"""Strict ``response_format`` contracts: admission, whole-answer validation, repair vocabulary."""

from theater.daemon.response_contracts.contract import (
    StrictContract,
    Verdict,
    admit_schema,
    parse_stored,
)
from theater.daemon.response_contracts.prompts import (
    prompt_with_guidance,
    repair_prompt,
    response_guidance,
)
from theater.daemon.response_contracts.registry import resolve_response_format

__all__ = [
    "StrictContract",
    "Verdict",
    "admit_schema",
    "parse_stored",
    "prompt_with_guidance",
    "repair_prompt",
    "resolve_response_format",
    "response_guidance",
]
