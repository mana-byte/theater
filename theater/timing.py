"""Compatibility facade: re-exports from observability.engine."""

from __future__ import annotations

import logging

from theater.constants.observability import (
    DEFAULT_SLOW_MS,
    GIT_MS,
    LAG_INTERVAL_S,
    LAG_WARN_S,
    PROC_MS,
    READY_LAG_MAX_S,
    TMUX_MS,
    WORKERS_MS,
)
from theater.observability.engine import (
    _render,
    emit,
    enable_trace,
    lag_monitor,
    ready_lag,
    span,
)

logger = logging.getLogger("theater.timing")

__all__ = [
    "DEFAULT_SLOW_MS",
    "GIT_MS",
    "LAG_INTERVAL_S",
    "LAG_WARN_S",
    "PROC_MS",
    "READY_LAG_MAX_S",
    "TMUX_MS",
    "WORKERS_MS",
    "_render",
    "emit",
    "enable_trace",
    "lag_monitor",
    "ready_lag",
    "span",
]
