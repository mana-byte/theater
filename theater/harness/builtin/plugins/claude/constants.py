"""Claude Code-native keys, markers, and bounded limits."""

from __future__ import annotations

CLAUDE_RECEIPT_COMMAND = "transcript-receipt"
CLAUDE_RECEIPT_EVENTS = ("SessionStart", "PreCompact")

IDLE_PROMPTS = (">", "> ")
APPROVAL_MARKER = "Esc to cancel"
TRUST_MARKER = "Yes, I trust this folder"
WORKING_MARKER = "esc to interrupt"
IDLE_FOOTER = "? for shortcuts"
IDLE_AGENTS_FOOTER = "← for agents"
MODE_LINE_PREFIXES = ("⏸", "⏵⏵")

_SCREEN_TAIL_LINES = 6
_CWD_PROBE_RECORDS = 20
_CWD_PROBE_BYTES = 256 * 1024
_LOSS_CANDIDATE_PROBES = 8

_WRITE_TOOLS: dict[str, str] = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
_READ_TOOLS: dict[str, str] = {"Read": "file_path"}
