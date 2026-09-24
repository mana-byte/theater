"""Pure bounded render helpers shared by trajectory widgets."""

from __future__ import annotations

from regie.trajectory.domain import (
    TimingProvenance,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryStatus,
)
from regie.trajectory.limits import (
    TRAJECTORY_THEATER_BUS_RECORD_PREFIX,
    TRAJECTORY_THEATER_BUS_SOURCE_EPOCH,
)
from regie.trajectory.rich.render.formatting import (
    format_duration,
    plain_text,
    sanitize_text,
    status_label,
)
from regie.trajectory.ui_constants import TRAJECTORY_OVERVIEW_COMPACT_NUMBER_THRESHOLD


def compact_number(value: int) -> str:
    """Render a non-negative count in compact notation."""
    if value < TRAJECTORY_OVERVIEW_COMPACT_NUMBER_THRESHOLD:
        return str(value)
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if value >= divisor:
            return f"{value / divisor:.1f}".rstrip("0").rstrip(".") + suffix
    return str(value)


def compact_cost(value: float) -> str:
    """Render a dollar value with compact precision."""
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


def is_raw_theater_bus_record(record: TrajectoryRecord) -> bool:
    """Theater's own diagnostic bus rows, which the timeline does not plot."""
    return (
        record.source_epoch == TRAJECTORY_THEATER_BUS_SOURCE_EPOCH
        and record.record_id.startswith(TRAJECTORY_THEATER_BUS_RECORD_PREFIX)
    )


def has_content(record: TrajectoryRecord) -> bool:
    """Whether a span has anything to show; empty ones are visual clutter on the timeline.

    Tool calls and unfinished spans count as content: their presence is the information.
    """
    return bool(
        record.summary.strip()
        or record.details
        or record.links
        or record.failure is not None
        or record.lane is TrajectoryLane.TOOLS
        or record.status in {TrajectoryStatus.PENDING, TrajectoryStatus.RUNNING}
    )


def supports_duration_interval(record: TrajectoryRecord) -> bool:
    """Whether a record has independently reported usable interval data."""
    timing = record.timing
    return (
        timing is not None
        and timing.provenance in {TimingProvenance.SOURCE, TimingProvenance.OBSERVED}
        and (
            timing.duration_ms is not None or (timing.start is not None and timing.end is not None)
        )
    )


__all__ = [
    "compact_cost",
    "compact_number",
    "format_duration",
    "has_content",
    "is_raw_theater_bus_record",
    "plain_text",
    "sanitize_text",
    "status_label",
    "supports_duration_interval",
]
