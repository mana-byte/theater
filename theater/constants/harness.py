"""Immutable constants for harness discovery and spawned harness processes."""

from __future__ import annotations

#: Name the theater MCP server is registered under inside each harness.
HARNESS_MCP_SERVER_NAME = "theater"

#: Per-tool MCP call timeout; 340 = MAX_AWAIT (300s) + 40s client slack. Duplicated, not imported.
HARNESS_MCP_TOOL_TIMEOUT_SECONDS = 340.0

#: No default anywhere: the caller must choose — the whole safety story.
HARNESS_APPROVAL_POLICIES = ("manual", "edits", "yolo")

#: Bus text clip limit; the transcript on disk remains the full record.
HARNESS_EVENT_TEXT_MAX_CHARS = 2000

#: Session name when no tmux session is requested or found.
SPAWN_FALLBACK_TMUX_SESSION = "theater"

#: Poll attempts when confirming a pane is gone after kill-pane.
SPAWN_KILL_POLL_ATTEMPTS = 5

#: Interval between kill-pane confirmation polls, in seconds.
SPAWN_KILL_POLL_INTERVAL_SECONDS = 0.25

#: Both tmux and Linux truncate observed process names to this length.
HARNESS_TMUX_OBSERVATION_NAME_LENGTH = 15

# Compatibility alias re-exported by the spawner façade.
FALLBACK_SESSION = SPAWN_FALLBACK_TMUX_SESSION
