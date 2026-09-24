"""Pure time-scaled lane layout for the trajectory timeline."""

from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from regie.trajectory.domain import Timing, TrajectoryKind, TrajectoryLane, TrajectoryRecord
from regie.trajectory.rich.enums import TimelineLane
from regie.trajectory.rich.render.records import supports_duration_interval
from regie.trajectory.ui_constants import (
    TIMELINE_BREAK_CELLS,
    TIMELINE_IDLE_GAP_CELLS,
    TIMELINE_MAX_LANE_ROWS,
    TIMELINE_SCALE_SEARCH_STEPS,
    TIMELINE_SPAN_MAX_CELLS,
    TIMELINE_SPAN_MIN_CELLS,
    TIMELINE_TARGET_SPAN_CELLS,
)

# Events that happen at an instant; they never borrow their request's interval.
POINT_EVENT_KINDS = frozenset(
    {
        TrajectoryKind.USER,
        TrajectoryKind.SYSTEM,
        TrajectoryKind.CONTEXT,
        TrajectoryKind.THEATER,
        TrajectoryKind.THEATER_CALL,
        TrajectoryKind.THEATER_RESULT,
        TrajectoryKind.SPAWN,
        TrajectoryKind.RESUME,
        TrajectoryKind.SEND,
        TrajectoryKind.RECEIVE,
        TrajectoryKind.KILL,
        TrajectoryKind.TRANSCRIPT_BOUNDARY,
        TrajectoryKind.SESSION_BOUNDARY,
        TrajectoryKind.OBSERVATION_ERROR,
    }
)


def timeline_lane(record: TrajectoryRecord) -> TimelineLane:
    if record.lane is TrajectoryLane.TOOLS and record.mcp_server is not None:
        return TimelineLane.MCP
    return TimelineLane(record.lane.value)


@dataclass(frozen=True, slots=True)
class TimelineSpan:
    """One record's cells; a point is a single cell with no duration."""

    record_id: str
    lane: TimelineLane
    x: int
    width: int
    point: bool
    row: int = 0

    @property
    def end(self) -> int:
        return self.x + self.width


@dataclass(frozen=True, slots=True)
class TimelineLayout:
    spans: tuple[TimelineSpan, ...]
    width: int
    rows: tuple[tuple[TimelineLane, int], ...] = ()
    # Cell ranges (start, end) where capped time was cut out of the axis.
    breaks: tuple[tuple[int, int], ...] = ()

    def rows_for(self, lane: TimelineLane) -> int:
        return dict(self.rows).get(lane, 1)

    def span_for(self, record_id: str | None) -> TimelineSpan | None:
        return next((span for span in self.spans if span.record_id == record_id), None)


def _bounds(timing: Timing | None) -> tuple[float, float] | None:
    if timing is None or timing.start is None:
        return None
    end = timing.end
    if end is None and timing.duration_ms is not None:
        end = timing.start + timing.duration_ms / 1_000
    return (timing.start, end) if end is not None and end >= timing.start else None


def _interval(
    record: TrajectoryRecord, timing_for: Callable[[str], Timing | None] | None
) -> tuple[float, float] | None:
    """A record's own reported interval, else its operation's derived interval."""
    if supports_duration_interval(record) and (own := _bounds(record.timing)) is not None:
        return own
    return _bounds(timing_for(record.record_id)) if timing_for is not None else None


def _instant(record: TrajectoryRecord) -> float | None:
    timing = record.timing
    if timing is None:
        return None
    return timing.start if timing.start is not None else timing.end


def _cells(deltas: Sequence[tuple[float, bool]], scale: float, cap: int) -> list[int]:
    # Busy stretches scale with time up to the cap; idle ones collapse to a small gap.
    cells = [max(1, round(dt * scale)) for dt, _busy in deltas]
    return [
        min(cap, count) if busy else min(TIMELINE_IDLE_GAP_CELLS, count)
        for count, (_dt, busy) in zip(cells, deltas, strict=True)
    ]


def _fit_scale(deltas: Sequence[tuple[float, bool]], width: int, cap: int) -> float:
    """The largest cells-per-second whose layout still fits the available width."""

    def fits(scale: float) -> bool:
        return sum(_cells(deltas, scale, cap)) + 1 <= width

    if not fits(0.0):
        return 0.0
    low, high = 0.0, 1.0
    while fits(high) and high < 1e9:
        low, high = high, high * 2
    for _ in range(TIMELINE_SCALE_SEARCH_STEPS):
        middle = (low + high) / 2
        low, high = (middle, high) if fits(middle) else (low, middle)
    return low


def _stack(
    spans: Sequence[TimelineSpan],
) -> tuple[tuple[TimelineSpan, ...], dict[TimelineLane, int]]:
    """Give each span the first row of its lane it fits in, so overlaps stack."""
    ends: dict[TimelineLane, list[int]] = {}
    rows: dict[str, int] = {}
    for span in sorted(spans, key=lambda span: (span.x, -span.width)):
        lane_ends = ends.setdefault(span.lane, [])
        row = next((index for index, end in enumerate(lane_ends) if end <= span.x), None)
        if row is None and len(lane_ends) < TIMELINE_MAX_LANE_ROWS:
            row = len(lane_ends)
            lane_ends.append(span.end)
        elif row is None:  # past the cap, share the row that frees up first
            row = min(range(len(lane_ends)), key=lane_ends.__getitem__)
        lane_ends[row] = max(lane_ends[row], span.end)
        rows[span.record_id] = row
    stacked = tuple(replace(span, row=rows[span.record_id]) for span in spans)
    return stacked, {lane: len(lane_ends) for lane, lane_ends in ends.items()}


def _readable_scale(times: Sequence[tuple[float, float, bool]]) -> float:
    """Cells per second that give the median span a comfortably readable width."""
    durations = sorted(end - start for start, end, point in times if not point)
    median = durations[len(durations) // 2] if durations else 0.0
    return TIMELINE_TARGET_SPAN_CELLS / median if median > 0 else 0.0


def build_timeline_layout(
    records: Sequence[TrajectoryRecord],
    *,
    minimum_width: int = 1,
    timing_for: Callable[[str], Timing | None] | None = None,
    zoom: float = 1.0,
) -> TimelineLayout:
    """Place records on a shared clock so span widths follow their durations.

    Records keep their chronological order; untimed records sit at the time of
    the record before them, and idle gaps between activity are compressed.
    Zoom 1 is the readable default; zooming out stops once everything fits.
    """
    times: list[tuple[float, float, bool]] = []
    previous = 0.0
    for record in records:
        interval = _interval(record, timing_for)
        if interval is not None:
            previous = interval[0]
            times.append((interval[0], interval[1], interval[1] <= interval[0]))
            continue
        instant = _instant(record)
        previous = previous if instant is None else max(previous, instant)
        times.append((previous, previous, True))
    if not times:
        return TimelineLayout((), max(1, minimum_width))
    edges = sorted({edge for start, end, _point in times for edge in (start, end)})
    busy_until = sorted((start, end) for start, end, point in times if not point)
    deltas: list[tuple[float, bool]] = []
    covered, cursor = float("-inf"), 0
    for left, right in itertools.pairwise(edges):
        while cursor < len(busy_until) and busy_until[cursor][0] <= left:
            covered = max(covered, busy_until[cursor][1])
            cursor += 1
        deltas.append((right - left, covered >= right))
    # Zooming in lets long spans grow; zooming out never squeezes them to one width.
    cap = round(TIMELINE_SPAN_MAX_CELLS * max(1.0, zoom))
    scale = max(_fit_scale(deltas, max(1, minimum_width), cap), _readable_scale(times) * zoom)
    x_for = {edges[0]: 0}
    x = 0
    breaks: list[tuple[int, int]] = []
    for edge, cells, (dt, busy) in zip(edges[1:], _cells(deltas, scale, cap), deltas, strict=False):
        if busy and round(dt * scale) > cells:  # capped: mark the cut in the middle
            start = x + (cells - TIMELINE_BREAK_CELLS) // 2
            breaks.append((start, start + TIMELINE_BREAK_CELLS))
        x += cells
        x_for[edge] = x
    spans = tuple(
        TimelineSpan(
            record.record_id,
            timeline_lane(record),
            x_for[start],
            1 if point else max(TIMELINE_SPAN_MIN_CELLS, x_for[end] - x_for[start]),
            point,
        )
        for record, (start, end, point) in zip(records, times, strict=True)
    )
    width = max(minimum_width, *(span.end for span in spans))
    stacked, rows = _stack(spans)
    return TimelineLayout(stacked, width, tuple(rows.items()), tuple(breaks))


__all__ = [
    "POINT_EVENT_KINDS",
    "TimelineLayout",
    "TimelineSpan",
    "build_timeline_layout",
    "timeline_lane",
]
