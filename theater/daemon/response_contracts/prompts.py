"""Prompt text for a stored ``response_format``: initial guidance and the repair request."""

from __future__ import annotations

from theater.daemon.response_contracts.contract import StrictContract, parse_stored

HINT_INSTRUCTION = (
    "Return your final answer as a single bare JSON value (no code fences, no prose) "
    "matching this schema hint: {schema}"
)
_STRICT_INSTRUCTION = (
    "Return your final answer as a single bare JSON value (no code fences, no prose) that "
    "validates against this JSON Schema. It is checked strictly: {schema}"
)
_REPAIR_INSTRUCTION = (
    "[Theater] Your last final answer was not accepted: {detail}. Reply now with only the "
    "corrected final answer: a single bare JSON value (no code fences, no prose) that "
    "validates against this JSON Schema: {schema}"
)


def response_guidance(response_format: str) -> str:
    """The line prepended to a prompt that carries ``response_format``."""
    contract = parse_stored(response_format)
    if contract is None:
        return HINT_INSTRUCTION.format(schema=response_format)
    return _STRICT_INSTRUCTION.format(schema=contract.schema_text)


def prompt_with_guidance(prompt: str, response_format: str) -> str:
    """Add the guidance to a prompt: a hint prepends, a strict contract trails the task.

    Completion matches a prompt by its first PROMPT_MATCH characters, so the constant strict
    guidance must not come first.
    """
    guidance = response_guidance(response_format)
    if parse_stored(response_format) is None:
        return f"{guidance}\n\n{prompt}"
    return f"{prompt}\n\n{guidance}" if prompt.strip() else guidance


def repair_prompt(contract: StrictContract, detail: str) -> str:
    return _REPAIR_INSTRUCTION.format(detail=detail, schema=contract.schema_text)
