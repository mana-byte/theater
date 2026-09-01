"""Interactive trajectory overview."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Callable, Sequence
from heapq import heappop, heappush
from typing import ClassVar

from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.geometry import Offset, Size
from textual.message import Message
from textual.scroll_view import ScrollView
from textual.strip import Strip

from theater.constants.regie_trajectory import (
    TIMELINE_CONTENT_HEIGHT,
    TIMELINE_HEIGHT,
    TIMELINE_LABEL_RIGHT_PADDING,
    TIMELINE_LABEL_WIDTH,
    TIMELINE_LANE_HEIGHT,
    TIMELINE_SPAN_MIN_WIDTH,
    TIMELINE_TURN_BOUNDARY_GLYPH,
)
from theater.regie.trajectory.enums import OrderMode, TimelineLane
from theater.regie.trajectory.render.timeline import (
    TimelineLayout,
    TimelineSpan,
    build_timeline_layout,
    timeline_lane,
)
from theater.trajectory import Timing, TrajectoryRecord, TrajectoryStatus


class TimelineSpanHovered(Message):
    """Pointer or keyboard moved over one timeline span."""

    def __init__(self, record_id: str | None) -> None:
        super().__init__()
        self.record_id = record_id


class TimelineSpanClicked(Message):
    """Pointer selected one timeline span."""

    def __init__(self, record_id: str | None) -> None:
        super().__init__()
        self.record_id = record_id


class TimelineScrolled(Message):
    """Horizontal timeline viewport changed."""

    def __init__(self, offset: int) -> None:
        super().__init__()
        self.offset = offset


class Timeline(ScrollView):
    """Render lane spans with Textual's line API and native scrolling."""

    can_focus = True
    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "trajectory-timeline--error",
        "trajectory-timeline--input",
        "trajectory-timeline--input-highlighted",
        "trajectory-timeline--label",
        "trajectory-timeline--model",
        "trajectory-timeline--model-highlighted",
        "trajectory-timeline--mcp",
        "trajectory-timeline--mcp-highlighted",
        "trajectory-timeline--muted",
        "trajectory-timeline--rail",
        "trajectory-timeline--running",
        "trajectory-timeline--running-highlighted",
        "trajectory-timeline--selected",
        "trajectory-timeline--theater",
        "trajectory-timeline--theater-highlighted",
        "trajectory-timeline--tools",
        "trajectory-timeline--tools-highlighted",
        "trajectory-timeline--track",
        "trajectory-timeline--turn",
    }

    DEFAULT_CSS = f"""
    Timeline {{
        width: 1fr;
        height: {TIMELINE_HEIGHT};
        min-height: {TIMELINE_HEIGHT};
        overflow-x: auto;
        overflow-y: hidden;
        scrollbar-size: 0 0;
        background: $background;
        border-bottom: solid $foreground 12%;
    }}
    Timeline:focus {{
        border-bottom: solid $accent 30%;
    }}
    Timeline > .trajectory-timeline--label {{ color: $text-muted; text-style: bold; }}
    Timeline > .trajectory-timeline--track {{ background: $background; }}
    Timeline > .trajectory-timeline--rail {{ background: $foreground 3%; }}
    Timeline > .trajectory-timeline--turn {{ color: $text-muted; text-style: bold; }}
    Timeline > .trajectory-timeline--input {{ background: $primary 28%; }}
    Timeline > .trajectory-timeline--input-highlighted {{ background: $primary; }}
    Timeline > .trajectory-timeline--model {{ background: $accent 28%; }}
    Timeline > .trajectory-timeline--model-highlighted {{ background: $accent; }}
    Timeline > .trajectory-timeline--tools {{ background: $warning 26%; }}
    Timeline > .trajectory-timeline--tools-highlighted {{ background: $warning; }}
    Timeline > .trajectory-timeline--mcp {{ background: $success 26%; }}
    Timeline > .trajectory-timeline--mcp-highlighted {{ background: $success; }}
    Timeline > .trajectory-timeline--theater {{ background: $secondary 26%; }}
    Timeline > .trajectory-timeline--theater-highlighted {{ background: $secondary; }}
    Timeline > .trajectory-timeline--error {{ background: $error; }}
    Timeline > .trajectory-timeline--running {{ background: $warning 32%; }}
    Timeline > .trajectory-timeline--running-highlighted {{ background: $warning; }}
    Timeline > .trajectory-timeline--muted {{ opacity: 32%; }}
    Timeline > .trajectory-timeline--selected {{
        background: $accent 40%;
    }}
    """

    _LANES = tuple(TimelineLane)

    def __init__(
        self,
        records: Sequence[TrajectoryRecord] = (),
        *,
        matched_ids: frozenset[str] | set[str] | None = None,
        selected_id: str | None = None,
        duration_mode: bool = False,
        scroll_offset: int = 0,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._records: tuple[TrajectoryRecord, ...] = ()
        self._records_by_id: dict[str, TrajectoryRecord] = {}
        self._span_ids: tuple[str, ...] = ()
        self._span_indices: dict[str, int] = {}
        self._span_index = 0
        self._hovered_id: str | None = None
        self._selected_id = selected_id
        self._matched_ids: frozenset[str] = frozenset()
        self._duration_mode = duration_mode
        self._timing_for: Callable[[str], Timing | None] | None = None
        self._layout = build_timeline_layout((), OrderMode.ORDER)
        self._span_by_id: dict[str, TimelineSpan] = {}
        self._lane_visual_segments: dict[
            TimelineLane, tuple[tuple[int, int, TimelineSpan], ...]
        ] = dict.fromkeys(self._LANES, ())
        self._lane_visual_ends: dict[TimelineLane, tuple[int, ...]] = dict.fromkeys(self._LANES, ())
        self._lane_hit_segments: dict[TimelineLane, tuple[tuple[int, int, TimelineSpan], ...]] = (
            dict.fromkeys(self._LANES, ())
        )
        self._lane_hit_ends: dict[TimelineLane, tuple[int, ...]] = dict.fromkeys(self._LANES, ())
        self._turn_boundaries: tuple[int, ...] = ()
        self._scroll_offset = max(0, int(scroll_offset))
        self._viewport_width = 0
        self.virtual_size = Size(TIMELINE_LABEL_WIDTH + 1, TIMELINE_CONTENT_HEIGHT)
        self.update_records(
            records,
            matched_ids=matched_ids,
            selected_id=selected_id,
            duration_mode=duration_mode,
            scroll_offset=scroll_offset,
        )

    @property
    def records(self) -> tuple[TrajectoryRecord, ...]:
        return self._records

    @property
    def hovered_id(self) -> str | None:
        return self._hovered_id

    @property
    def selected_id(self) -> str | None:
        return self._selected_id

    @property
    def span_ids(self) -> tuple[str, ...]:
        return self._span_ids

    @property
    def duration_mode(self) -> bool:
        return self._duration_mode

    @property
    def projection(self) -> TimelineLayout:
        return self._layout

    @property
    def horizontal_offset(self) -> int:
        return self._scroll_offset

    @property
    def tail_offset(self) -> int:
        return max(0, self._layout.width - self._available_cells())

    def _component(self, name: str) -> Style:
        return self.get_component_rich_style(f"trajectory-timeline--{name}")

    def _lane_style(self, lane: TimelineLane, *, highlighted: bool = False) -> Style:
        suffix = "-highlighted" if highlighted else ""
        return self._component(f"{lane.value}{suffix}")

    def _highlighted_id(self) -> str | None:
        return self._hovered_id or self._selected_id

    def _span_style(self, record: TrajectoryRecord) -> Style:
        highlighted = record.record_id == self._highlighted_id()
        style = self._lane_style(timeline_lane(record), highlighted=highlighted)
        if record.status in {TrajectoryStatus.ERROR, TrajectoryStatus.INTERRUPTED}:
            style += self._component("error")
        elif record.status in {TrajectoryStatus.PENDING, TrajectoryStatus.RUNNING}:
            style += self._component("running-highlighted" if highlighted else "running")
        if record.record_id not in self._matched_ids and not highlighted:
            style += self._component("muted")
        if record.record_id == self._selected_id and not highlighted:
            style += self._component("selected")
        return style

    def _lane_row(self, y: int) -> tuple[TimelineLane, int] | None:
        if y < 0:
            return None
        lane_index, row = divmod(y, TIMELINE_LANE_HEIGHT)
        if lane_index >= len(self._LANES):
            return None
        return self._LANES[lane_index], row

    def _label(self, y: int) -> tuple[str, Style]:
        lane_row = self._lane_row(y)
        if lane_row is not None:
            lane, row = lane_row
            label = lane.value.upper() if row == TIMELINE_LANE_HEIGHT // 2 else ""
            return label, self._component("label")
        return "", self._component("label")

    @staticmethod
    def _styled_strip(characters: list[str], styles: list[Style], width: int) -> Strip:
        if not styles:
            return Strip.blank(width)
        segments: list[Segment] = []
        start = 0
        active = styles[0]
        for index, style in enumerate(styles[1:], start=1):
            if style == active:
                continue
            segments.append(Segment("".join(characters[start:index]), active))
            start = index
            active = style
        segments.append(Segment("".join(characters[start:width]), active))
        return Strip(segments, width)

    def _lane_strip(
        self,
        lane: TimelineLane,
        start: int,
        width: int,
        row: int = TIMELINE_LANE_HEIGHT // 2,
    ) -> Strip:
        characters = [" "] * width
        paints_spans = row == TIMELINE_LANE_HEIGHT // 2
        base_style = self._component("rail" if paints_spans else "track")
        styles = [base_style] * width
        if paints_spans:
            segments = self._lane_visual_segments[lane]
            ends = self._lane_visual_ends[lane]
            index = bisect_right(ends, start)
            end = start + width
            while index < len(segments):
                segment_start, segment_end, span = segments[index]
                if segment_start >= end:
                    break
                record = self._records_by_id.get(span.record_id)
                if record is not None:
                    style = self._span_style(record)
                    for x in range(
                        max(0, segment_start - start),
                        min(width, segment_end - start),
                    ):
                        styles[x] = style
                index += 1
            highlighted_id = self._highlighted_id()
            highlighted = self._span_by_id.get(highlighted_id or "")
            if highlighted is not None and highlighted.lane is lane:
                record = self._records_by_id.get(highlighted.record_id)
                if record is not None:
                    style = self._span_style(record)
                    for x in range(
                        max(0, highlighted.x - start),
                        min(width, highlighted.end - start),
                    ):
                        styles[x] = style
        first_boundary = bisect_left(self._turn_boundaries, start)
        last_boundary = bisect_left(self._turn_boundaries, start + width)
        boundary_style = self._component("turn")
        for boundary in self._turn_boundaries[first_boundary:last_boundary]:
            offset = boundary - start
            characters[offset] = TIMELINE_TURN_BOUNDARY_GLYPH
            styles[offset] += boundary_style
        return self._styled_strip(characters, styles, width)

    def render_line(self, y: int) -> Strip:
        scroll_x, scroll_y = self.scroll_offset
        y += int(scroll_y)
        width = max(1, self.size.width)
        label_width = min(TIMELINE_LABEL_WIDTH, max(1, width - 1))
        chart_width = max(1, width - label_width)
        if y >= TIMELINE_CONTENT_HEIGHT:
            return Strip.blank(width, self.rich_style)
        label, label_style = self._label(y)
        label_padding = min(TIMELINE_LABEL_RIGHT_PADDING, label_width)
        label_text_width = label_width - label_padding
        label_text = label[:label_text_width].rjust(label_text_width).ljust(label_width)
        label_strip = Strip([Segment(label_text, label_style)], label_width)
        chart_start = int(scroll_x)
        if (lane_row := self._lane_row(y)) is not None:
            lane, row = lane_row
            chart = self._lane_strip(lane, chart_start, chart_width, row)
        else:
            chart = Strip.blank(chart_width, self._component("track"))
        return Strip.join((label_strip, chart))

    def _index_spans(self) -> None:
        self._span_by_id = {span.record_id: span for span in self._layout.spans}
        seen_turns: set[tuple[str, str]] = set()
        boundaries: set[int] = set()
        for record in self._records:
            if record.turn_id is None:
                continue
            turn = (record.source_epoch, record.turn_id)
            if turn in seen_turns:
                continue
            seen_turns.add(turn)
            span = self._span_by_id.get(record.record_id)
            if span is not None and span.x > 0:
                boundaries.add(span.x)
        self._turn_boundaries = tuple(sorted(boundaries))
        for lane in self._LANES:
            spans = tuple(
                sorted(
                    (span for span in self._layout.spans if span.lane is lane),
                    key=lambda span: span.x,
                )
            )
            visual_segments = self._winning_segments(spans, visual=True, prefer_later=True)
            self._lane_visual_segments[lane] = visual_segments
            self._lane_visual_ends[lane] = tuple(segment[1] for segment in visual_segments)
            hit_segments = self._winning_segments(spans, visual=False, prefer_later=False)
            self._lane_hit_segments[lane] = hit_segments
            self._lane_hit_ends[lane] = tuple(segment[1] for segment in hit_segments)

    @staticmethod
    def _winning_segments(
        spans: tuple[TimelineSpan, ...],
        *,
        visual: bool,
        prefer_later: bool,
    ) -> tuple[tuple[int, int, TimelineSpan], ...]:
        """Precompute the visible winner for each non-overlapping span range."""
        intervals = sorted(
            (
                (
                    span.visual_start if visual else span.x,
                    span.visual_end if visual else span.end,
                    ordinal,
                    span,
                )
                for ordinal, span in enumerate(spans)
                if (span.visual_start if visual else span.x)
                < (span.visual_end if visual else span.end)
            ),
            key=lambda item: (item[0], item[1], item[2]),
        )
        if not intervals:
            return ()
        positions = sorted(
            {edge for start, end, _ordinal, _span in intervals for edge in (start, end)}
        )
        active: list[tuple[int, int, int, int, TimelineSpan]] = []
        segments: list[tuple[int, int, TimelineSpan]] = []
        next_interval = 0
        previous: int | None = None
        for position in positions:
            if previous is not None and previous < position and active:
                winner = active[0][-1]
                if segments and segments[-1][1] == previous and segments[-1][2] == winner:
                    segment_start, _segment_end, _span = segments[-1]
                    segments[-1] = (segment_start, position, winner)
                else:
                    segments.append((previous, position, winner))
            while next_interval < len(intervals) and intervals[next_interval][0] == position:
                _start, end, ordinal, span = intervals[next_interval]
                tie_breaker = -ordinal if prefer_later else ordinal
                heappush(active, (span.width, -span.x, tie_breaker, end, span))
                next_interval += 1
            while active and active[0][3] <= position:
                heappop(active)
            previous = position
        return tuple(segments)

    def _visible_anchor(self) -> tuple[str, int] | None:
        candidates = [span for span in self._layout.spans if span.end > self._scroll_offset]
        if not candidates:
            return None
        span = min(candidates, key=lambda item: (item.x, item.width))
        return span.record_id, span.x - self._scroll_offset

    def update_records(
        self,
        records: Sequence[TrajectoryRecord],
        *,
        matched_ids: frozenset[str] | set[str] | None = None,
        hovered_id: str | None = None,
        selected_id: str | None = None,
        duration_mode: bool = False,
        scroll_offset: int | None = None,
        timing_for: Callable[[str], Timing | None] | None = None,
    ) -> None:
        old_anchor = self._visible_anchor()
        old_offset = self._scroll_offset
        self._records = tuple(records)
        self._records_by_id = {record.record_id: record for record in self._records}
        self._span_ids = tuple(record.record_id for record in self._records)
        self._span_indices = {record_id: index for index, record_id in enumerate(self._span_ids)}
        self._matched_ids = (
            frozenset(self._span_ids) if matched_ids is None else frozenset(matched_ids)
        )
        self._selected_id = selected_id
        self._hovered_id = hovered_id if hovered_id in self._records_by_id else None
        self._duration_mode = duration_mode
        self._timing_for = timing_for
        mode = OrderMode.DURATION if duration_mode else OrderMode.ORDER
        self._layout = build_timeline_layout(
            self._records,
            mode,
            minimum_width=self._available_cells(),
            timing_for=timing_for,
        )
        self._index_spans()
        if selected_id in self._span_indices:
            self._span_index = self._span_indices[selected_id]
        self._span_index = min(self._span_index, max(0, len(self._span_ids) - 1))
        self.virtual_size = Size(TIMELINE_LABEL_WIDTH + self._layout.width, TIMELINE_CONTENT_HEIGHT)
        requested = old_offset if scroll_offset is None else int(scroll_offset)
        if scroll_offset is None and old_anchor is not None:
            anchor_id, screen_x = old_anchor
            new_anchor = self._span_by_id.get(anchor_id)
            if new_anchor is not None:
                requested = new_anchor.x - screen_x
        self.set_scroll_offset(requested, repaint=False)
        self.refresh()

    def _available_cells(self) -> int:
        width = self._viewport_width or self.size.width or self.region.width
        return max(1, width - TIMELINE_LABEL_WIDTH)

    def set_scroll_offset(self, offset: int, *, repaint: bool = True) -> int:
        if isinstance(offset, bool):
            raise TypeError("timeline scroll offset must be an integer")
        self._scroll_offset = max(0, min(int(offset), self.tail_offset))
        if self.is_mounted:
            self.scroll_to(x=self._scroll_offset, animate=False, force=True)
        if repaint:
            self.refresh()
        return self._scroll_offset

    def scroll_to_tail(self, *, repaint: bool = True) -> int:
        return self.set_scroll_offset(self.tail_offset, repaint=repaint)

    def scroll_span_into_view(self, record_id: str | None) -> int:
        span = self._span_by_id.get(record_id or "")
        if span is None:
            return self._scroll_offset
        width = self._available_cells()
        if span.x < self._scroll_offset:
            self.set_scroll_offset(span.x)
        elif span.end > self._scroll_offset + width:
            self.set_scroll_offset(span.end - width)
        return self._scroll_offset

    def _record_at(self, x: int, y: int) -> TrajectoryRecord | None:
        lane_row = self._lane_row(y)
        if lane_row is None:
            return None
        chart_x = x - TIMELINE_LABEL_WIDTH + self._scroll_offset
        if chart_x < 0:
            return None
        lane, _row = lane_row
        segments = self._lane_hit_segments[lane]
        index = bisect_right(self._lane_hit_ends[lane], chart_x)
        if index >= len(segments):
            return None
        segment_start, segment_end, span = segments[index]
        if not segment_start <= chart_x < segment_end:
            return None
        return self._records_by_id.get(span.record_id)

    def hover_anchor(self, record_id: str | None) -> Offset | None:
        span = self._span_by_id.get(record_id or "")
        if span is None:
            return None
        viewport_start = self._scroll_offset
        viewport_end = viewport_start + self._available_cells()
        visible_start = max(span.visual_start, viewport_start)
        visible_end = min(span.visual_end, viewport_end)
        if visible_start >= visible_end:
            return None
        label_width = min(TIMELINE_LABEL_WIDTH, max(1, self.size.width - 1))
        center = (visible_start + visible_end - 1) // 2
        return Offset(
            self.content_region.x + label_width + center - viewport_start,
            self.content_region.y,
        )

    def _set_hover(
        self,
        record: TrajectoryRecord | None,
        *,
        notify: bool = True,
    ) -> None:
        record_id = record.record_id if record else None
        if record_id == self._hovered_id:
            return
        self._hovered_id = record_id
        if record_id is not None:
            self._span_index = self._span_indices[record_id]
        self.refresh()
        if notify:
            self.post_message(TimelineSpanHovered(record_id))

    def set_hovered(self, record_id: str | None) -> None:
        self._set_hover(self._records_by_id.get(record_id or ""), notify=False)

    def set_selected(self, record_id: str | None) -> None:
        selected_id = record_id if record_id in self._span_indices else None
        if selected_id == self._selected_id:
            return
        self._selected_id = selected_id
        if selected_id is not None:
            self._span_index = self._span_indices[selected_id]
        self.refresh()

    def move_span(self, delta: int) -> str | None:
        if not self._span_ids:
            return None
        self._span_index = max(0, min(len(self._span_ids) - 1, self._span_index + delta))
        record = self._records[self._span_index]
        self._set_hover(record)
        self.scroll_span_into_view(record.record_id)
        return record.record_id

    def select_span(self, record_id: str | None) -> None:
        if record_id not in self._span_indices:
            return
        self.set_selected(record_id)
        self.scroll_span_into_view(record_id)
        self._set_hover(self._records[self._span_index])

    def on_resize(self, event: events.Resize) -> None:
        was_at_tail = self._scroll_offset == self.tail_offset
        old_width = self._available_cells()
        self._viewport_width = max(1, event.size.width)
        if self._available_cells() != old_width:
            self.update_records(
                self._records,
                matched_ids=self._matched_ids,
                hovered_id=self._hovered_id,
                selected_id=self._selected_id,
                duration_mode=self._duration_mode,
                timing_for=self._timing_for,
            )
            if was_at_tail:
                self.scroll_to_tail()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        self._set_hover(self._record_at(int(event.x), int(event.y)))

    def on_leave(self, _event: events.Leave) -> None:
        self._set_hover(None)

    def on_click(self, event: events.Click) -> None:
        record = self._record_at(int(event.x), int(event.y))
        if record is None:
            return
        event.stop()
        self._set_hover(record)
        self._selected_id = record.record_id
        self.refresh()
        self.post_message(TimelineSpanClicked(record.record_id))

    def on_mouse_scroll_left(self, event: events.MouseScrollLeft) -> None:
        event.stop()
        self.set_scroll_offset(self._scroll_offset - TIMELINE_SPAN_MIN_WIDTH)

    def on_mouse_scroll_right(self, event: events.MouseScrollRight) -> None:
        event.stop()
        self.set_scroll_offset(self._scroll_offset + TIMELINE_SPAN_MIN_WIDTH)

    def watch_scroll_x(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_x(old_value, new_value)
        self._scroll_offset = max(0, min(int(new_value), self.tail_offset))
        if self.is_mounted:
            self.post_message(TimelineScrolled(self._scroll_offset))


__all__ = [
    "Timeline",
    "TimelineScrolled",
    "TimelineSpanClicked",
    "TimelineSpanHovered",
]
