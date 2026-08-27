"""Mistral Vibe plugin entrypoint."""

from __future__ import annotations

from theater.harness.builtin.adapters.vibe.constants import (
    _MARKER_KEY,
    _MARKER_VERSION,
    _READ_TOOLS,
    _SCAN_LIMIT,
    _SCREEN_IDLE_PROMPTS,
    _SCREEN_TAIL_LINES,
    _SPINNER_TAIL_LINES,
    _WRITE_TOOLS,
    APPROVAL_MARKER,
    IDLE_PROMPTS,
    ISOLATION_MARKER,
    TRUST_MARKER,
    VIBE_ACTIVE_MODEL_CONFIG_KEY,
    WORKING_MARKER,
    WORKING_MARKER_KEY,
)
from theater.harness.builtin.adapters.vibe.isolation import (
    _canonical,
    _marker_key,
    _marker_key_readonly,
    _marker_mac,
    _marker_mac_readonly,
    isolation_marker_text,
    validate_isolated_domain,
)
from theater.harness.builtin.adapters.vibe.launch import VibeHarness
from theater.harness.builtin.adapters.vibe.observer import VibeObserver
from theater.harness.builtin.adapters.vibe.screen import _in_screen_tail
from theater.harness.builtin.adapters.vibe.source import _VibeSource, _VibeTranscriptSource
from theater.harness.builtin.adapters.vibe.trajectory import (
    _extract_paths,
    _relativise,
    _vibe_detail,
    _vibe_duration,
    _vibe_fact,
    _vibe_identifier,
    _vibe_mcp_identity,
    _vibe_message_id,
    _vibe_path_details,
    _vibe_presentation,
    _vibe_tagged_text,
    _vibe_text,
)

__all__ = [
    "APPROVAL_MARKER",
    "HARNESS",
    "IDLE_PROMPTS",
    "ISOLATION_MARKER",
    "TRUST_MARKER",
    "VIBE_ACTIVE_MODEL_CONFIG_KEY",
    "WORKING_MARKER",
    "WORKING_MARKER_KEY",
    "_MARKER_KEY",
    "_MARKER_VERSION",
    "_READ_TOOLS",
    "_SCAN_LIMIT",
    "_SCREEN_IDLE_PROMPTS",
    "_SCREEN_TAIL_LINES",
    "_SPINNER_TAIL_LINES",
    "_WRITE_TOOLS",
    "VibeHarness",
    "VibeObserver",
    "_VibeSource",
    "_VibeTranscriptSource",
    "_canonical",
    "_extract_paths",
    "_in_screen_tail",
    "_marker_key",
    "_marker_key_readonly",
    "_marker_mac",
    "_marker_mac_readonly",
    "_relativise",
    "_vibe_detail",
    "_vibe_duration",
    "_vibe_fact",
    "_vibe_identifier",
    "_vibe_mcp_identity",
    "_vibe_message_id",
    "_vibe_path_details",
    "_vibe_presentation",
    "_vibe_tagged_text",
    "_vibe_text",
    "isolation_marker_text",
    "validate_isolated_domain",
]


#: What the loader looks for. An instance, not the class: see docs/harness-plugins.md.
HARNESS = VibeHarness()
