"""Interactive, time-scaled trajectory timeline."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Callable, Sequence
from heapq import heappop, heappush
from typing import ClassVar

from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.geometry import Size
from textual.message import Message
from textual.scroll_view import ScrollView
from textual.strip import Strip

from regie.trajectory.rich.enums import TimelineLane
from regie.trajectory.rich.render.timeline import (
    TimelineLayout,
    TimelineSpan,
    build_timeline_layout,
)
from regie.trajectory.ui_constants import (
    TIMELINE_BREAK,
    TIMELINE_GLYPH_BODY,
    TIMELINE_GLYPH_END,
    TIMELINE_GLYPH_POINT,
    TIMELINE_GLYPH_RAIL,
    TIMELINE_GLYPH_START,
    TIMELINE_GLYPH_TURN,
    TIMELINE_HEIGHT,
    TIMELINE_LABEL_RIGHT_PADDING,
    TIMELINE_LABEL_WIDTH,
    TIMELINE_LANE_COLORS,
    TIMELINE_SCROLL_STEP,
)
from theater.frontend.trajectory import Timing, TrajectoryRecord, TrajectoryStatus

Segments = tuple[tuple[int, int, TimelineSpan], ...]
# A lane's bar row (0, 1, …), a gap row (None), or the extra blank row above the lane.
LANE_GAP = -1
Track = tuple[TimelineLane, int | None]

# Spans and their lane label share one hue so the labels double as a legend.
_LANE_CSS = "\n".join(
    f"    Timeline > .trajectory-timeline--{lane} {{ color: {color}; }}\n"
    f"    Timeline > .trajectory-timeline--{lane}-label {{ color: {color}; text-style: bold; }}"
    for lane, color in TIMELINE_LANE_COLORS.items()
)


class TimelineSpanHovered(Message):
    """Pointer moved over one timeline span."""

    def __init__(self, record_id: str | None) -> None:
        super().__init__()
        self.record_id = record_id


class TimelineSpanClicked(Message):
    """Pointer selected one timeline span."""

    def __init__(self, record_id: str | None, *, open_details: bool = False) -> None:
        super().__init__()
        self.record_id = record_id
        self.open_details = open_details


class TimelineScrolled(Message):
    """Horizontal timeline viewport changed."""

    def __init__(self, offset: int) -> None:
        super().__init__()
        self.offset = offset


class Timeline(ScrollView):
    """Lanes of capped span bars whose widths follow elapsed time."""

    can_focus = True
    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "trajectory-timeline--rail",
        "trajectory-timeline--turn",
        *(f"trajectory-timeline--{lane}" for lane in TIMELINE_LANE_COLORS),
        *(f"trajectory-timeline--{lane}-label" for lane in TIMELINE_LANE_COLORS),
        "trajectory-timeline--error",
        "trajectory-timeline--running",
        "trajectory-timeline--muted",
        "trajectory-timeline--hovered",
        "trajectory-timeline--selected",
    }

    DEFAULT_CSS = f"""
    Timeline {{
        width: 1fr;
        height: {TIMELINE_HEIGHT};
        overflow-x: auto;
        overflow-y: hidden;
        scrollbar-size: 0 0;
        background: $background;
        margin-bottom: 1;
    }}
    Timeline > .trajectory-timeline--rail {{ color: $foreground 8%; }}
    Timeline > .trajectory-timeline--turn {{ color: $foreground 25%; }}
{_LANE_CSS}
    Timeline > .trajectory-timeline--error {{ color: $error; }}
    Timeline > .trajectory-timeline--running {{ text-style: italic; }}
    Timeline > .trajectory-timeline--muted {{ color: $foreground 20%; }}
    Timeline > .trajectory-timeline--hovered {{ background: $foreground 10%; }}
    Timeline > .trajectory-timeline--selected {{
        background: $foreground 22%;
        text-style: bold;
    }}
    """

    _LANES = tuple(TimelineLane)

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._records: tuple[TrajectoryRecord, ...] = ()
        self._records_by_id: dict[str, TrajectoryRecord] = {}
        self._span_ids: tuple[str, ...] = ()
        self._span_indices: dict[str, int] = {}
        self._hovered_id: str | None = None
        self._selected_id: str | None = None
        self._matched_ids: frozenset[str] = frozenset()
        self._layout = TimelineLayout((), 1)
        self._layout_key: tuple[object, ...] | None = None
        self._span_by_id: dict[str, TimelineSpan] = {}
        self._grid: tuple[Track, ...] = self._build_grid({})
        self._segments: dict[tuple[TimelineLane, int], Segments] = {}
        self._segment_ends: dict[tuple[TimelineLane, int], tuple[int, ...]] = {}
        self._turn_boundaries: tuple[int, ...] = ()
        self._scroll_offset = 0
        self._viewport_width = 0
        self._zoom = 1.0
        self._timing_for: Callable[[str], Timing | None] | None = None
        self.virtual_size = Size(TIMELINE_LABEL_WIDTH + 1, self.content_height)

    # ---- read-only state -----------------------------------------------------------

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
    def projection(self) -> TimelineLayout:
        return self._layout

    @property
    def zoom(self) -> float:
        return self._zoom

    def set_zoom(self, zoom: float) -> None:
        """Rescale time, keeping the selected span in view."""
        if zoom == self._zoom:
            return
        self._zoom = zoom
        self.update_records(
            self._records,
            matched_ids=self._matched_ids,
            selected_id=self._selected_id,
            timing_for=self._timing_for,
        )
        self.scroll_span_into_view(self._selected_id)

    @property
    def content_height(self) -> int:
        return len(self._grid)

    def track_y(self, lane: TimelineLane, row: int = 0) -> int:
        """The screen row of a lane's bar row."""
        return self._grid.index((lane, row))

    @classmethod
    def _build_grid(cls, rows: dict[TimelineLane, int]) -> tuple[Track, ...]:
        grid: list[Track] = []
        for index, lane in enumerate(cls._LANES):
            if index:
                grid.append((lane, LANE_GAP))
            for row in range(rows.get(lane, 1)):
                grid.extend(((lane, None), (lane, row)))
        return tuple(grid)

    @property
    def horizontal_offset(self) -> int:
        return self._scroll_offset

    @property
    def tail_offset(self) -> int:
        return max(0, self._layout.width - self._available_cells())

    # ---- rendering -------------------------------------------------------------------

    def _component(self, name: str) -> Style:
        return self.get_component_rich_style(f"trajectory-timeline--{name}")

    def _span_style(self, span: TimelineSpan) -> Style:
        record = self._records_by_id[span.record_id]
        if record.status in {TrajectoryStatus.ERROR, TrajectoryStatus.INTERRUPTED}:
            style = self._component("error")
        elif record.record_id in self._matched_ids:
            style = self._component(span.lane.value)
        else:
            style = self._component("muted")
        if record.status in {TrajectoryStatus.PENDING, TrajectoryStatus.RUNNING}:
            style += self._component("running")
        if record.record_id == self._selected_id:
            style += self._component("selected")
        elif record.record_id == self._hovered_id:
            style += self._component("hovered")
        return style

    def _glyph(self, span: TimelineSpan, x: int) -> str:
        if span.point:
            return TIMELINE_GLYPH_POINT
        if x == span.x:
            return TIMELINE_GLYPH_START
        if x == span.end - 1:
            return TIMELINE_GLYPH_END
        breaks = self._layout.breaks
        index = bisect_right(breaks, (x, float("inf"))) - 1
        if index >= 0 and breaks[index][0] <= x < breaks[index][1]:
            return TIMELINE_BREAK[x - breaks[index][0]]
        return TIMELINE_GLYPH_BODY

    def _lane_row(self, y: int) -> Track | None:
        return self._grid[y] if 0 <= y < len(self._grid) else None

    def _lane_strip(self, lane: TimelineLane, start: int, width: int, row: int | None = 0) -> Strip:
        """One track's cells: a bar row of the lane, or its gap row when row is None."""
        rail = self._component("rail")
        characters = [" "] * width
        styles = [rail] * width
        end = start + width
        if row is not None:
            characters = [TIMELINE_GLYPH_RAIL] * width
            segments = self._segments.get((lane, row), ())
            index = bisect_right(self._segment_ends.get((lane, row), ()), start)
            while index < len(segments) and segments[index][0] < end:
                segment_start, segment_end, span = segments[index]
                style = self._span_style(span)
                for x in range(max(start, segment_start), min(end, segment_end)):
                    characters[x - start] = self._glyph(span, x)
                    styles[x - start] = style
                index += 1
            # The selection draws last, so an overlapping span can never hide the cursor.
            selected = self._span_by_id.get(self._selected_id or "")
            if selected is not None and (selected.lane, selected.row) == (lane, row):
                style = self._span_style(selected)
                for x in range(max(start, selected.x), min(end, selected.end)):
                    characters[x - start] = self._glyph(selected, x)
                    styles[x - start] = style
        turn = self._component("turn")
        first = bisect_left(self._turn_boundaries, start)
        for boundary in self._turn_boundaries[first : bisect_left(self._turn_boundaries, end)]:
            if styles[boundary - start] is rail:
                characters[boundary - start] = TIMELINE_GLYPH_TURN
                styles[boundary - start] = turn
        runs: list[Segment] = []
        run_start = 0
        for index in range(1, width + 1):
            if index == width or styles[index] != styles[run_start]:
                runs.append(Segment("".join(characters[run_start:index]), styles[run_start]))
                run_start = index
        return Strip(runs, width)

    def render_line(self, y: int) -> Strip:
        scroll_x, scroll_y = self.scroll_offset
        width = max(1, self.size.width)
        label_width = min(TIMELINE_LABEL_WIDTH, max(1, width - 1))
        lane_row = self._lane_row(y + int(scroll_y))
        if lane_row is None:
            return Strip.blank(width, self.rich_style)
        lane, row = lane_row
        if row == LANE_GAP:
            row = None  # a blank gap row, so turn boundaries stay unbroken across lanes
        text = lane.value.upper() if row == 0 else ""
        label = text.rjust(label_width - TIMELINE_LABEL_RIGHT_PADDING).ljust(label_width)
        chart = self._lane_strip(lane, int(scroll_x), max(1, width - label_width), row)
        label_style = self._component(f"{lane.value}-label")
        return Strip.join((Strip([Segment(label, label_style)], label_width), chart))

    # ---- layout ---------------------------------------------------------------------

    def _index_spans(self) -> None:
        self._span_by_id = {span.record_id: span for span in self._layout.spans}
        seen: set[tuple[str, str]] = set()
        boundaries: set[int] = set()
        for record in self._records:
            turn = (record.source_epoch, record.turn_id or "")
            if record.turn_id is not None and turn not in seen:
                seen.add(turn)
                span = self._span_by_id.get(record.record_id)
                if span is not None and span.x > 0:
                    boundaries.add(span.x - 1)
        self._turn_boundaries = tuple(sorted(boundaries))
        self._grid = self._build_grid(dict(self._layout.rows))
        self._segments = {
            (lane, row): self._winning_segments(
                tuple(span for span in self._layout.spans if (span.lane, span.row) == (lane, row))
            )
            for lane, row in self._grid
            if row is not None and row != LANE_GAP
        }
        self._segment_ends = {
            track: tuple(segment[1] for segment in segments)
            for track, segments in self._segments.items()
        }
        # Concurrent spans add rows, so the timeline grows to show every row.
        self.styles.height = self.content_height + 1

    @staticmethod
    def _winning_segments(spans: tuple[TimelineSpan, ...]) -> Segments:
        """Split a lane into ranges, each owned by its narrowest (then latest) span."""
        intervals = sorted(
            ((span.x, span.end, ordinal, span) for ordinal, span in enumerate(spans)),
            key=lambda item: (item[0], item[1], item[2]),
        )
        positions = sorted({edge for start, end, _o, _s in intervals for edge in (start, end)})
        active: list[tuple[int, int, int, int, TimelineSpan]] = []
        segments: list[tuple[int, int, TimelineSpan]] = []
        cursor = 0
        previous: int | None = None
        for position in positions:
            if previous is not None and active:
                winner = active[0][-1]
                if segments and segments[-1][1] == previous and segments[-1][2] is winner:
                    segments[-1] = (segments[-1][0], position, winner)
                else:
                    segments.append((previous, position, winner))
            while cursor < len(intervals) and intervals[cursor][0] == position:
                _start, end, ordinal, span = intervals[cursor]
                heappush(active, (span.width, -span.x, -ordinal, end, span))
                cursor += 1
            while active and active[0][3] <= position:
                heappop(active)
            previous = position
        return tuple(segments)

    def update_records(
        self,
        records: Sequence[TrajectoryRecord],
        *,
        matched_ids: frozenset[str] | None = None,
        selected_id: str | None = None,
        scroll_offset: int | None = None,
        timing_for: Callable[[str], Timing | None] | None = None,
    ) -> None:
        self._records = tuple(records)
        self._records_by_id = {record.record_id: record for record in self._records}
        self._span_ids = tuple(self._records_by_id)
        self._span_indices = {record_id: index for index, record_id in enumerate(self._span_ids)}
        self._matched_ids = frozenset(self._span_ids) if matched_ids is None else matched_ids
        self._selected_id = selected_id
        if self._hovered_id not in self._records_by_id:
            self._hovered_id = None
        self._timing_for = timing_for or self._timing_for
        key = (
            tuple((record.record_id, record.revision) for record in self._records),
            self._available_cells(),
            self._zoom,
        )
        if key != self._layout_key:
            self._layout_key = key
            self._layout = build_timeline_layout(
                self._records,
                minimum_width=self._available_cells(),
                timing_for=self._timing_for,
                zoom=self._zoom,
            )
            self._index_spans()
        self.virtual_size = Size(TIMELINE_LABEL_WIDTH + self._layout.width, self.content_height)
        self.set_scroll_offset(
            self._scroll_offset if scroll_offset is None else scroll_offset, repaint=False
        )
        self.refresh()

    def _available_cells(self) -> int:
        width = self._viewport_width or self.size.width or self.region.width
        return max(1, width - TIMELINE_LABEL_WIDTH)

    # ---- scrolling and selection ---------------------------------------------------------

    def set_scroll_offset(self, offset: int, *, repaint: bool = True) -> int:
        self._scroll_offset = max(0, min(int(offset), self.tail_offset))
        if self.is_mounted:
            self.scroll_to(x=self._scroll_offset, animate=False, force=True)
        if repaint:
            self.refresh()
        return self._scroll_offset

    def scroll_to_tail(self, *, repaint: bool = True) -> int:
        return self.set_scroll_offset(self.tail_offset, repaint=repaint)

    def scroll_span_into_view(self, record_id: str | None) -> int:
        """Center the span; the edges of the run clamp it, so ends stay flush."""
        span = self._span_by_id.get(record_id or "")
        if span is not None:
            self.set_scroll_offset((span.x + span.end - self._available_cells()) // 2)
        return self._scroll_offset

    def _record_at(self, x: int, y: int) -> TrajectoryRecord | None:
        lane_row = self._lane_row(y)
        chart_x = x - TIMELINE_LABEL_WIDTH + self._scroll_offset
        if lane_row is None or lane_row[1] is None or chart_x < 0:
            return None
        track = (lane_row[0], lane_row[1])
        segments = self._segments.get(track, ())
        index = bisect_right(self._segment_ends.get(track, ()), chart_x)
        if index >= len(segments) or not segments[index][0] <= chart_x < segments[index][1]:
            return None
        return self._records_by_id.get(segments[index][2].record_id)

    def _set_hover(self, record: TrajectoryRecord | None, *, notify: bool = True) -> None:
        record_id = record.record_id if record else None
        if record_id == self._hovered_id:
            return
        self._hovered_id = record_id
        self.refresh()
        if notify:
            self.post_message(TimelineSpanHovered(record_id))

    def set_hovered(self, record_id: str | None) -> None:
        self._set_hover(self._records_by_id.get(record_id or ""), notify=False)

    def set_selected(self, record_id: str | None) -> None:
        if record_id not in self._span_indices or record_id == self._selected_id:
            return
        self._selected_id = record_id
        self.refresh()

    def _lane_span_ids(self, lane: TimelineLane) -> tuple[str, ...]:
        return tuple(span.record_id for span in self._layout.spans if span.lane is lane)

    def _select_span(self, record_id: str) -> str:
        self.set_selected(record_id)
        self.scroll_span_into_view(record_id)
        return record_id

    def move_span(self, delta: int) -> str | None:
        """Step through time within the selected span's lane only."""
        current = self._span_by_id.get(self._selected_id or "")
        if current is None:
            return self._select_span(self._span_ids[-1]) if self._span_ids else None
        lane_ids = self._lane_span_ids(current.lane)
        index = lane_ids.index(current.record_id) + delta
        return self._select_span(lane_ids[max(0, min(len(lane_ids) - 1, index))])

    def move_lane(self, delta: int) -> str | None:
        """Focus the next populated lane above or below, on its span nearest in time."""
        current = self._span_by_id.get(self._selected_id or "")
        if current is None:
            return self.move_span(0)
        center = (current.x + current.end) / 2
        index = self._LANES.index(current.lane) + delta
        while 0 <= index < len(self._LANES):
            spans = [span for span in self._layout.spans if span.lane is self._LANES[index]]
            if spans:
                target = min(spans, key=lambda span: abs((span.x + span.end) / 2 - center))
                return self._select_span(target.record_id)
            index += delta
        return current.record_id

    def is_lane_start(self, record_id: str | None) -> bool:
        span = self._span_by_id.get(record_id or "")
        return span is not None and self._lane_span_ids(span.lane)[0] == span.record_id

    # ---- events ---------------------------------------------------------------------------

    def on_resize(self, event: events.Resize) -> None:
        was_at_tail = self._scroll_offset == self.tail_offset
        self._viewport_width = max(1, event.size.width)
        self._layout_key = None
        self.update_records(
            self._records,
            matched_ids=self._matched_ids,
            selected_id=self._selected_id,
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
        self.set_selected(record.record_id)
        self.post_message(TimelineSpanClicked(record.record_id, open_details=event.chain >= 2))

    def on_mouse_scroll_left(self, event: events.MouseScrollLeft) -> None:
        event.stop()
        self.set_scroll_offset(self._scroll_offset - TIMELINE_SCROLL_STEP)

    def on_mouse_scroll_right(self, event: events.MouseScrollRight) -> None:
        event.stop()
        self.set_scroll_offset(self._scroll_offset + TIMELINE_SCROLL_STEP)

    def watch_scroll_x(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_x(old_value, new_value)
        self._scroll_offset = max(0, min(int(new_value), self.tail_offset))
        if self.is_mounted:
            self.post_message(TimelineScrolled(self._scroll_offset))


__all__ = ["Timeline", "TimelineScrolled", "TimelineSpanClicked", "TimelineSpanHovered"]
