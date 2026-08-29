"""Codex-native record names and bounded adapter values."""

from __future__ import annotations

import re

CODEX_BINARY = "codex"
CODEX_SESSION_META_RECORD_TYPE = "session_meta"
CODEX_THREAD_SETTINGS_EVENT_TYPE = "thread_settings_applied"
CODEX_MODEL_PROVIDER_ID_KEY = "model_provider_id"
CODEX_MODEL_PROVIDER_KEY = "model_provider"

PROMPT = "\u203a"
WORKING_MARKER = "esc to interrupt"
APPROVAL_MARKER = "to cancel"
TRUST_MARKER = "Do you trust the contents"
_SCREEN_TAIL_LINES = 5
_CWD_PROBE_BYTES = 256 * 1024
_ROLLOUT_METADATA_CACHE_SIZE = 512
_LOSS_CANDIDATE_PROBES = 8
_STEM = re.compile(r"^rollout-\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d-(.+)$")
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.MULTILINE)
