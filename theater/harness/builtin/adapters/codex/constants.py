from __future__ import annotations

import re

CODEX_SESSION_META_RECORD_TYPE = "session_meta"
CODEX_THREAD_SETTINGS_EVENT_TYPE = "thread_settings_applied"
CODEX_MODEL_PROVIDER_ID_KEY = "model_provider_id"
CODEX_MODEL_PROVIDER_KEY = "model_provider"

#: The composer prompt. A single glyph (U+203A), not the ASCII ">" that Claude Code uses.
PROMPT = "\u203a"

#: Present in the status bar while a turn runs. Codex keeps a persistent footer.
WORKING_MARKER = "esc to interrupt"

#: Approval overlay and MCP/auth prompts. NOT `to confirm`: the `/approvals` popup renders that.
APPROVAL_MARKER = "to cancel"

#: First-launch trust dialog. Whole-capture, not tail-scoped: body text above the rows.
TRUST_MARKER = "Do you trust the contents"

#: How far up from the bottom to look for the composer.
_SCREEN_TAIL_LINES = 5

#: `session_meta` is the first record and carries `cwd`; probed by reading exactly one line.
_CWD_PROBE_BYTES = 256 * 1024

#: Bound record reads during heuristic loss detection; only the newest few files are opened for cwd.
_LOSS_CANDIDATE_PROBES = 8

#: Filename is `rollout-<ISO with - separators>-<uuid>`. Anchoring on timestamp keeps uuid hyphens.
_STEM = re.compile(r"^rollout-\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d-(.+)$")

#: apply_patch hunks are delimited by these markers. Paths after them are repo-relative.
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.MULTILINE)
