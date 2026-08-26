"""Pure lane and time projection for the trajectory overview."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil

from theater.constants.regie_trajectory import (
    TIMELINE_DURATION_MIN_WIDTH,
    TIMELINE_DURATION_UNTIMED_GAP,
    TIMELINE_SPAN_GUTTER,
    TIMELINE_SPAN_MIN_WIDTH,
)
from theater.regie.trajectory.enums import OrderMode
from theater.regie.trajectory.render.records import supports_duration_interval
from theater.trajectory import TrajectoryLane, TrajectoryRecord


@dataclass(frozen=True, slots=True)
class TimelineSpan:
    record_id: str
    lane: TrajectoryLane
    x: int
    width: int
    timed: bool

    @property
    def end(self) -> int:
        return self.x + self.width

    @property
    def visual_start(self) -> int:
        return self.x + min(TIMELINE_SPAN_GUTTER, max(0, (self.width - 1) // 2))

    @property
    def visual_end(self) -> int:
        return self.end - min(TIMELINE_SPAN_GUTTER, max(0, self.width // 2))


@dataclass(frozen=True, slots=True)
class TimelineLayout:
    spans: tuple[TimelineSpan, ...]
    width: int
    mode: OrderMode
    has_timing: bool

    def span_for(self, record_id: str | None) -> TimelineSpan | None:
        if record_id is None:
            return None
        return next((span for span in self.spans if span.record_id == record_id), None)

    def record_at(self, x: int, lane: TrajectoryLane) -> str | None:
        matches = [
            span
            for span in self.spans
            if span.lane is lane and span.visual_start <= x < span.visual_end
        ]
        if not matches:
            return None
        return min(matches, key=lambda span: (span.width, -span.x)).record_id


def _sequence_layout(records: tuple[TrajectoryRecord, ...], minimum_width: int) -> TimelineLayout:
    count = len(records)
    width = max(1, minimum_width, count * TIMELINE_SPAN_MIN_WIDTH)
    spans = (
        tuple(
            TimelineSpan(
                record_id=record.record_id,
                lane=record.lane,
                x=index * width // count,
                width=max(1, (index + 1) * width // count - index * width // count),
                timed=False,
            )
            for index, record in enumerate(records)
        )
        if count
        else ()
    )
    return TimelineLayout(
        spans=spans,
        width=width,
        mode=OrderMode.ORDER,
        has_timing=False,
    )


def _interval(record: TrajectoryRecord) -> tuple[float, float] | None:
    timing = record.timing
    if timing is None or timing.start is None or not supports_duration_interval(record):
        return None
    end = timing.end
    if end is None and timing.duration_ms is not None:
        end = timing.start + timing.duration_ms / 1_000
    if end is None or end < timing.start:
        return None
    return timing.start, end


def _duration_layout(records: tuple[TrajectoryRecord, ...], minimum_width: int) -> TimelineLayout:
    raw = [(record, interval) for record in records if (interval := _interval(record))]
    if not raw:
        fallback = _sequence_layout(records, minimum_width)
        return TimelineLayout(
            spans=fallback.spans,
            width=fallback.width,
            mode=OrderMode.DURATION,
            has_timing=False,
        )

    removed_by_id: dict[str, float] = {}
    removed_idle = 0.0
    covered_until: float | None = None
    for record, (start, end) in sorted(raw, key=lambda item: (item[1][0], item[1][1])):
        if covered_until is not None and start > covered_until:
            removed_idle += start - covered_until
        removed_by_id[record.record_id] = removed_idle
        covered_until = end if covered_until is None else max(covered_until, end)

    projected = {
        record.record_id: (
            start - removed_by_id[record.record_id],
            end - removed_by_id[record.record_id],
        )
        for record, (start, end) in raw
    }
    domain_start = min(start for start, _ in projected.values())
    domain_end = max(end for _, end in projected.values())
    domain = max(domain_end - domain_start, 0.001)
    untimed = [record for record in records if record.record_id not in projected]
    untimed_width = len(untimed) * TIMELINE_SPAN_MIN_WIDTH
    untimed_space = untimed_width + (TIMELINE_DURATION_UNTIMED_GAP if untimed else 0)
    timed_width = max(
        TIMELINE_DURATION_MIN_WIDTH,
        len(raw) * TIMELINE_SPAN_MIN_WIDTH,
        minimum_width - untimed_space,
    )

    spans: list[TimelineSpan] = []
    for record in records:
        interval = projected.get(record.record_id)
        if interval is None:
            continue
        start, end = interval
        x = round((start - domain_start) / domain * (timed_width - 1))
        width = max(TIMELINE_SPAN_MIN_WIDTH, ceil((end - start) / domain * timed_width))
        spans.append(TimelineSpan(record.record_id, record.lane, x, width, True))

    untimed_start = timed_width + (TIMELINE_DURATION_UNTIMED_GAP if untimed else 0)
    spans.extend(
        TimelineSpan(
            record.record_id,
            record.lane,
            untimed_start + index * TIMELINE_SPAN_MIN_WIDTH,
            TIMELINE_SPAN_MIN_WIDTH,
            False,
        )
        for index, record in enumerate(untimed)
    )
    spans_by_id = {span.record_id: span for span in spans}
    ordered_spans = tuple(spans_by_id[record.record_id] for record in records)
    width = max(
        timed_width,
        untimed_start + len(untimed) * TIMELINE_SPAN_MIN_WIDTH,
        max((span.end for span in spans), default=1),
    )
    return TimelineLayout(
        spans=ordered_spans,
        width=width,
        mode=OrderMode.DURATION,
        has_timing=True,
    )


def build_timeline_layout(
    records: tuple[TrajectoryRecord, ...],
    mode: OrderMode,
    *,
    minimum_width: int = 1,
) -> TimelineLayout:
    minimum_width = max(1, int(minimum_width))
    if mode is OrderMode.DURATION:
        return _duration_layout(records, minimum_width)
    return _sequence_layout(records, minimum_width)


__all__ = [
    "TimelineLayout",
    "TimelineSpan",
    "build_timeline_layout",
]
