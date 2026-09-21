"""Bounded, thread-safe suppression of repeated transcript-discovery warnings."""

from __future__ import annotations

import logging
from collections import OrderedDict
from pathlib import Path
from threading import Lock
from time import monotonic

from theater.constants.observability import (
    DISCOVERY_WARNING_INTERVAL_S,
    DISCOVERY_WARNING_SCOPE_LIMIT,
)

_warnings: OrderedDict[tuple[str, str, str, str], tuple[int, float, int]] = OrderedDict()
_lock = Lock()


def report_discovery_matches(
    logger: logging.Logger, message: str, *, root: Path, cwd: str, count: int
) -> None:
    """Warn on new collisions and periodically summarize unchanged repeats."""
    key = (logger.name, message, str(root), cwd)
    now = monotonic()
    with _lock:
        previous = _warnings.pop(key, None)
        if count <= 1:
            return
        suppressed = previous[2] if previous is not None else 0
        if (
            previous is not None
            and count == previous[0]
            and now - previous[1] < DISCOVERY_WARNING_INTERVAL_S
        ):
            _warnings[key] = (count, previous[1], suppressed + 1)
            return
        _warnings[key] = (count, now, 0)
        while len(_warnings) > DISCOVERY_WARNING_SCOPE_LIMIT:
            _warnings.popitem(last=False)
    if suppressed:
        logger.warning(message + " (%d repeated warnings suppressed)", count, cwd, suppressed)
    else:
        logger.warning(message, count, cwd)
