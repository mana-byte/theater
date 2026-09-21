"""Distinguish monotonic operation timing from wall-clock trace envelopes."""

from __future__ import annotations

from theater.constants.observability import CLOCK_GAP_WARN_MS


def timing_attributes(elapsed_ms: float, wall_ms: float) -> dict[str, float | bool]:
    """A clock gap can indicate suspend or a clock adjustment, never proven CPU work."""
    gap = wall_ms - elapsed_ms
    return {
        "theater.duration_ms": elapsed_ms,
        "theater.wall_duration_ms": wall_ms,
        "theater.clock_gap_ms": gap,
        "theater.clock_discontinuity": abs(gap) >= CLOCK_GAP_WARN_MS,
    }
