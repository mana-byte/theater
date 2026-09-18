"""Small Régie-owned timing context used by presentation hot paths."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def span(_operation: str, **_attributes: object) -> Iterator[dict[str, object]]:
    """Keep presentation instrumentation local without importing daemon telemetry."""
    yield {}


REGIE_TRAJECTORY_DETAIL_PROJECT = "regie.trajectory.detail.project"
REGIE_TRAJECTORY_DETAIL_RENDER = "regie.trajectory.detail.render"

__all__ = ["REGIE_TRAJECTORY_DETAIL_PROJECT", "REGIE_TRAJECTORY_DETAIL_RENDER", "span"]
