"""Strict ``response_format`` vocabulary shared by the daemon and the MCP tool descriptions."""

from __future__ import annotations

import re

#: The reserved key that turns a ``response_format`` dict from a hint into a strict contract.
CONTRACT_TAG = "$theater"

#: Built-in presets, in the order the tool descriptions list them.
CONTRACT_PRESET_NAMES = ("review", "debate", "delegate")
CONTRACT_KIND_CUSTOM = "custom"
#: Required fields of each preset, as the tool descriptions list them.
CONTRACT_PRESET_SUMMARY = {
    "review": "verdict, reviewed, summary, findings",
    "debate": (
        "verdict, position, strongest_evidence, weakest_assumption, position_change, "
        "remaining_blocker"
    ),
    "delegate": "status, summary, blockers",
}

CONTRACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
#: Registered custom names one creator may hold at once.
CONTRACT_MAX_CUSTOM_PER_CREATOR = 16
#: Canonical serialized schema bytes accepted at admission.
CONTRACT_SCHEMA_MAX_BYTES = 32 * 1024
#: Final answers longer than this are never parsed; it matches the native result bound.
CONTRACT_ANSWER_MAX_CHARS = 65_536
#: Preset answers are the caller's whole input, so they stay short; custom ones keep the cap above.
CONTRACT_PRESET_ANSWER_MAX_CHARS = 16_384
#: Schema violations quoted back to the agent in a repair prompt.
CONTRACT_ERRORS_MAX = 5
CONTRACT_DETAIL_MAX_CHARS = 1_000
CONTRACT_AUDIT_DETAIL_MAX_CHARS = 500

#: Repair prompts sent per public job before it ends ``unable_to_parse``.
REPAIR_MAX_ATTEMPTS = 2
#: Wall-clock bound on one job's whole repair, from its first attempt through every state.
REPAIR_DEADLINE_SECONDS = 300.0

#: The only public outcome of a strict job that never produced a valid answer.
STRUCTURED_UNABLE_TO_PARSE = "unable_to_parse"

#: The only keywords a strict schema may use. Without references or branching, each answer value
#: meets exactly one schema node, so validation cost is bounded by the answer and the caps below.
CONTRACT_ALLOWED_KEYWORDS = frozenset(
    {
        "type",
        "enum",
        "const",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "prefixItems",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minProperties",
        "maxProperties",
        # Annotations; none is asserted.
        "title",
        "description",
        "default",
        "examples",
        "deprecated",
        "readOnly",
        "writeOnly",
        "$comment",
        "format",
    }
)
#: Schema nesting (JSON levels), and per-node list sizes every answer value is checked against.
CONTRACT_SCHEMA_MAX_DEPTH = 24
CONTRACT_SCHEMA_MAX_LIST = 64
#: Values in one parsed answer; a dense 64 Ki answer could otherwise hold tens of thousands.
CONTRACT_ANSWER_MAX_VALUES = 5_000
CONTRACT_DRAFT_URI = "https://json-schema.org/draft/2020-12/schema"

__all__ = [
    "CONTRACT_ALLOWED_KEYWORDS",
    "CONTRACT_ANSWER_MAX_CHARS",
    "CONTRACT_ANSWER_MAX_VALUES",
    "CONTRACT_AUDIT_DETAIL_MAX_CHARS",
    "CONTRACT_DETAIL_MAX_CHARS",
    "CONTRACT_DRAFT_URI",
    "CONTRACT_ERRORS_MAX",
    "CONTRACT_KIND_CUSTOM",
    "CONTRACT_MAX_CUSTOM_PER_CREATOR",
    "CONTRACT_NAME",
    "CONTRACT_PRESET_ANSWER_MAX_CHARS",
    "CONTRACT_PRESET_NAMES",
    "CONTRACT_PRESET_SUMMARY",
    "CONTRACT_SCHEMA_MAX_BYTES",
    "CONTRACT_SCHEMA_MAX_DEPTH",
    "CONTRACT_SCHEMA_MAX_LIST",
    "CONTRACT_TAG",
    "REPAIR_DEADLINE_SECONDS",
    "REPAIR_MAX_ATTEMPTS",
    "STRUCTURED_UNABLE_TO_PARSE",
]
