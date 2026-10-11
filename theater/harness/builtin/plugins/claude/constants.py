"""Claude Code-native keys, markers, and bounded limits."""

from __future__ import annotations

from typing import Final

CLAUDE_RECEIPT_EVENTS = ("SessionStart", "PreCompact")
CLAUDE_THINKING_SUMMARIES_KEY: Final = "showThinkingSummaries"

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
_RESUME_CWD_SUFFIX_BYTES = 256 * 1024
_RESUME_CWD_SUFFIX_RECORDS = 128
_LOSS_CANDIDATE_PROBES = 8

_WRITE_TOOLS: dict[str, str] = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
_READ_TOOLS: dict[str, str] = {"Read": "file_path"}

#: Oldest CLI whose checked-in fixture carries the causal ``turn_duration`` completion marker.
SEAL_MIN_VERSION: Final = (2, 1, 220)
#: Records and messages remembered to resolve a marker's parent chain across transcript reads.
SEAL_NODE_LIMIT: Final = 2048
SEAL_MESSAGE_LIMIT: Final = 32
#: Non-assistant records (an attachment) allowed between a marker and its final assistant record.
SEAL_MAX_HOPS: Final = 4
