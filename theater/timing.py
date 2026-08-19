"""Centralized latency instrumentation for daemon operations."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Iterator, MutableMapping
from typing import Any

logger = logging.getLogger("theater.timing")

DEFAULT_SLOW_MS = 250.0
TMUX_MS = 100.0
GIT_MS = 200.0
PROC_MS = 50.0
WORKERS_MS = 500.0

#: Past this, a readiness lag is not a spawn measurement: every participant is
#: re-watched when the daemon restarts, and `created_at` can be hours old.
READY_LAG_MAX_S = 60.0
LAG_INTERVAL_S = 0.5
LAG_WARN_S = 0.25


def _render(name: str, ms: float, fields: MutableMapping[str, Any]) -> str:
    tail = "".join(f" {key}={value}" for key, value in fields.items() if value is not None)
    return f"{name} {ms:.1f}ms{tail}"


def emit(name: str, ms: float, *, slow_ms: float = DEFAULT_SLOW_MS, **fields: Any) -> None:
    """Log a duration measured somewhere other than a block."""
    if ms >= slow_ms:
        logger.info("%s", _render(name, ms, fields))
    elif logger.isEnabledFor(logging.DEBUG):
        logger.debug("%s", _render(name, ms, fields))


@contextlib.contextmanager
def span(name: str, *, slow_ms: float = DEFAULT_SLOW_MS, **fields: Any) -> Iterator[dict[str, Any]]:
    """Time a block, including failures, and yield fields callers may extend."""
    started = time.perf_counter()
    try:
        yield fields
    finally:
        emit(name, (time.perf_counter() - started) * 1000.0, slow_ms=slow_ms, **fields)


def ready_lag(name: str, pid: str, created_at: float | None, **fields: Any) -> None:
    """Log a participant milestone measured across separate loops."""
    if created_at is None:
        return
    lag = time.time() - created_at
    if not 0.0 <= lag <= READY_LAG_MAX_S:
        return
    emit(name, lag * 1000.0, id=pid, **fields)


async def lag_monitor(stopping: asyncio.Event) -> None:
    """Warn when event-loop wake-up exceeds the lag budget."""
    loop = asyncio.get_running_loop()
    while not stopping.is_set():
        before = loop.time()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), timeout=LAG_INTERVAL_S)
        lag = loop.time() - before - LAG_INTERVAL_S
        if lag >= LAG_WARN_S:
            logger.warning(
                "event loop blocked for %.0fms — every agent's call and every "
                "observer poll waited that long; look for synchronous work "
                "(git, lsof, a large sweep) in the timing log just above",
                lag * 1000,
            )


def enable_trace() -> None:
    """Enable full timing traces without enabling unrelated debug logs."""
    logger.setLevel(logging.DEBUG)
