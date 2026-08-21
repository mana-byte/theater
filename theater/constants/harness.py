"""Immutable harness spawn constants.

Fallback tmux session name and kill-poll timing used by the spawning service.
"""

from __future__ import annotations

#: Session name when no tmux session is requested or found.
SPAWN_FALLBACK_TMUX_SESSION = "theater"

#: Poll attempts when confirming a pane is gone after kill-pane.
SPAWN_KILL_POLL_ATTEMPTS = 5

#: Interval between kill-pane confirmation polls, in seconds.
SPAWN_KILL_POLL_INTERVAL_SECONDS = 0.25

# Compatibility alias re-exported by the spawner façade.
FALLBACK_SESSION = SPAWN_FALLBACK_TMUX_SESSION
