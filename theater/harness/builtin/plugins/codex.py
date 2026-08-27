"""OpenAI Codex CLI harness entrypoint."""

import json

from theater.harness.builtin.adapters.codex.constants import (
    _CWD_PROBE_BYTES,
    _LOSS_CANDIDATE_PROBES,
    _PATCH_FILE_RE,
    _SCREEN_TAIL_LINES,
    _STEM,
    APPROVAL_MARKER,
    CODEX_MODEL_PROVIDER_ID_KEY,
    CODEX_MODEL_PROVIDER_KEY,
    CODEX_SESSION_META_RECORD_TYPE,
    CODEX_THREAD_SETTINGS_EVENT_TYPE,
    PROMPT,
    TRUST_MARKER,
    WORKING_MARKER,
)
from theater.harness.builtin.adapters.codex.identity import _is_codex, _resolve
from theater.harness.builtin.adapters.codex.launch import CodexHarness
from theater.harness.builtin.adapters.codex.observer import CodexObserver
from theater.harness.builtin.adapters.codex.screen import _in_screen_tail
from theater.harness.builtin.adapters.codex.source import _CodexSource
from theater.harness.builtin.adapters.codex.trajectory import (
    _apply_patch_paths,
    _codex_block_id,
    _codex_content_text,
    _codex_duration,
    _codex_mcp_identity,
    _codex_revision,
    _codex_scoped_id,
    _codex_timing,
    _codex_trajectory_turn_id,
    _codex_usage,
    _epoch,
    _event_path,
    _flatten,
    _patch_change_paths,
    _safe_trajectory_text,
    _stable_json,
    _trajectory_detail,
    _trajectory_float,
    _trajectory_id,
    _trajectory_int,
    _trajectory_status,
    _trajectory_time,
    _turn_id,
)

#: What the loader looks for. An instance, not the class (see docs/harness-plugins.md).
HARNESS = CodexHarness()

__all__ = [
    "APPROVAL_MARKER",
    "CODEX_MODEL_PROVIDER_ID_KEY",
    "CODEX_MODEL_PROVIDER_KEY",
    "CODEX_SESSION_META_RECORD_TYPE",
    "CODEX_THREAD_SETTINGS_EVENT_TYPE",
    "HARNESS",
    "PROMPT",
    "TRUST_MARKER",
    "WORKING_MARKER",
    "_CWD_PROBE_BYTES",
    "_LOSS_CANDIDATE_PROBES",
    "_PATCH_FILE_RE",
    "_SCREEN_TAIL_LINES",
    "_STEM",
    "CodexHarness",
    "CodexObserver",
    "_CodexSource",
    "_apply_patch_paths",
    "_codex_block_id",
    "_codex_content_text",
    "_codex_duration",
    "_codex_mcp_identity",
    "_codex_revision",
    "_codex_scoped_id",
    "_codex_timing",
    "_codex_trajectory_turn_id",
    "_codex_usage",
    "_epoch",
    "_event_path",
    "_flatten",
    "_in_screen_tail",
    "_is_codex",
    "_patch_change_paths",
    "_resolve",
    "_safe_trajectory_text",
    "_stable_json",
    "_trajectory_detail",
    "_trajectory_float",
    "_trajectory_id",
    "_trajectory_int",
    "_trajectory_status",
    "_trajectory_time",
    "_turn_id",
    "json",
]
