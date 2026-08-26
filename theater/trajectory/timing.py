"""Timing endpoint helpers shared by request and tool projections."""

from __future__ import annotations

from theater.trajectory.enums import TimingProvenance
from theater.trajectory.records import Timing


def terminal_timestamp(timing: Timing) -> float | None:
    """Treat a terminal point timestamp as an end when no interval was supplied."""
    return timing.end if timing.end is not None else timing.start


def derived_interval(
    start_timing: Timing,
    end_timing: Timing,
    *,
    first_token: float | None = None,
) -> Timing | None:
    """Build an interval, marking any observation-derived endpoint as estimated."""
    start = start_timing.start
    end = terminal_timestamp(end_timing)
    if start is None or end is None or end <= start:
        return None
    provenance = (
        TimingProvenance.OBSERVED
        if TimingProvenance.OBSERVED in {start_timing.provenance, end_timing.provenance}
        else TimingProvenance.DERIVED
    )
    if first_token is not None and not start <= first_token <= end:
        first_token = None
    return Timing(
        start=start,
        end=end,
        duration_ms=(end - start) * 1000,
        provenance=provenance,
        first_token=first_token,
    )


__all__ = ["derived_interval", "terminal_timestamp"]
