"""Fixed CLI constants: ANSI control sequences, batch sizes, and timeouts."""

from __future__ import annotations

#: Home + clear-screen ANSI sequence for redraw loops.
CLI_CLEAR_SCREEN = "\033[H\033[2J"

#: How many bus events to pull per follow tick.
CLI_FOLLOW_BATCH_SIZE = 200

#: How long to wait for a stopping daemon to release socket and lock.
CLI_STOP_TIMEOUT_SECONDS = 5.0
