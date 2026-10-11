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

#: The only public outcome of a strict job that never produced a valid answer.
STRUCTURED_UNABLE_TO_PARSE = "unable_to_parse"

#: Schema keywords refused because they run regexes or quadratic scans on the daemon loop, or
#: reach outside the schema.
CONTRACT_FORBIDDEN_KEYWORDS = frozenset(
    {
        "pattern",
        "patternProperties",
        "uniqueItems",
        "$id",
        "$dynamicRef",
        "$recursiveRef",
        "$dynamicAnchor",
    }
)
CONTRACT_DRAFT_URI = "https://json-schema.org/draft/2020-12/schema"

__all__ = [
    "CONTRACT_ANSWER_MAX_CHARS",
    "CONTRACT_AUDIT_DETAIL_MAX_CHARS",
    "CONTRACT_DETAIL_MAX_CHARS",
    "CONTRACT_DRAFT_URI",
    "CONTRACT_ERRORS_MAX",
    "CONTRACT_FORBIDDEN_KEYWORDS",
    "CONTRACT_KIND_CUSTOM",
    "CONTRACT_MAX_CUSTOM_PER_CREATOR",
    "CONTRACT_NAME",
    "CONTRACT_PRESET_ANSWER_MAX_CHARS",
    "CONTRACT_PRESET_NAMES",
    "CONTRACT_PRESET_SUMMARY",
    "CONTRACT_SCHEMA_MAX_BYTES",
    "CONTRACT_TAG",
    "REPAIR_MAX_ATTEMPTS",
    "STRUCTURED_UNABLE_TO_PARSE",
]
