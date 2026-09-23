"""Presentation-only defaults retained by the independent Régie package."""

from __future__ import annotations

REGIE_TREE_INTERVAL_SECONDS = 1.0
REGIE_BUS_INTERVAL_SECONDS = 0.4
REGIE_BUS_BATCH = 50
REGIE_ACTION_HISTORY_LIMIT = 100
#: Settled action lanes kept connected for reuse by the next action.
REGIE_IDLE_ACTION_CLIENTS = 1
REGIE_SIDEBAR_WIDTH = 52
REGIE_TRAJECTORY_PAGE_SIZE = 30
REGIE_STAGEABLE_PROVIDER_KIND = "tmux"
REGIE_REQUIRED_CAPABILITIES = (
    "orchestration.v1",
    "state.follow.v1",
    "catalogs.v1",
    "diagnostics.v1",
    "trajectory.v1",
)

__all__ = [
    "REGIE_ACTION_HISTORY_LIMIT",
    "REGIE_BUS_BATCH",
    "REGIE_BUS_INTERVAL_SECONDS",
    "REGIE_IDLE_ACTION_CLIENTS",
    "REGIE_REQUIRED_CAPABILITIES",
    "REGIE_SIDEBAR_WIDTH",
    "REGIE_STAGEABLE_PROVIDER_KIND",
    "REGIE_TRAJECTORY_PAGE_SIZE",
    "REGIE_TREE_INTERVAL_SECONDS",
]
