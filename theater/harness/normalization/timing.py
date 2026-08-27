"""Shared ISO-8601 timestamp conversion and timing assembly."""

from __future__ import annotations

from datetime import datetime

from theater.harness.normalization.values import finite_float
from theater.trajectory.enums import TimingProvenance
from theater.trajectory.records import Timing


def iso_epoch(value: object) -> float | None:
    """Return an ISO-8601 timestamp as epoch seconds."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def epoch_or_number(value: object) -> float | None:
    """Return an ISO timestamp epoch or a finite float."""
    if isinstance(value, str):
        return iso_epoch(value)
    return finite_float(value)


def assemble_timing(
    start: float | None,
    end: float | None,
    duration_ms: float | None,
    *,
    first_token: float | None = None,
    provenance: TimingProvenance,
) -> Timing | None:
    """Assemble a Timing from a start/end/duration triple with gap-filling.

    Invariants: all-None returns None; end < start drops end; the single
    missing member of the start/end/duration triple is filled; first_token
    later than end is dropped.
    """
    if start is None and end is None and duration_ms is None:
        return None
    if start is not None and end is not None and end < start:
        end = None
    if duration_ms is not None:
        if start is None and end is not None:
            start = end - duration_ms / 1_000
        elif end is None and start is not None:
            end = start + duration_ms / 1_000
    elif start is not None and end is not None:
        duration_ms = (end - start) * 1_000
    if first_token is not None and end is not None and first_token > end:
        first_token = None
    try:
        return Timing(
            start=start,
            end=end,
            first_token=first_token,
            duration_ms=duration_ms,
            provenance=provenance,
        )
    except ValueError:
        return None
