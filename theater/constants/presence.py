"""Immutable presence-policy constants shared by the tmux and daemon layers."""

from __future__ import annotations

#: Periodic inventory refresh bound; hooks only accelerate it.
PRESENCE_REFRESH_INTERVAL_SECONDS = 2.0

#: Bounded wait for monitor tasks to cancel and reap during aclose.
PRESENCE_CLOSE_TIMEOUT_SECONDS = 5.0

#: A cached inventory older than this fails every snapshot closed.
PRESENCE_INVENTORY_STALE_SECONDS = 5.0

#: Bound for one owned inventory observation, independent of callers.
PRESENCE_REFRESH_TIMEOUT_SECONDS = 5.0
