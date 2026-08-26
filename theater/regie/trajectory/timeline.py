"""Interactive four-lane trajectory overview."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
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
    TIMELINE_HOVER_LEFT_GLYPH,
    TIMELINE_HOVER_RIGHT_GLYPH,
    TIMELINE_HOVER_SINGLE_GLYPH,
    TIMELINE_LABEL_RIGHT_PADDING,
    TIMELINE_LABEL_WIDTH,
    TIMELINE_LANE_HEIGHT,
    TIMELINE_RELATED_GLYPH,
    TIMELINE_SPAN_MIN_WIDTH,
    TIMELINE_TURN_BOUNDARY_GLYPH,
)
from theater.regie.trajectory.enums import OrderMode
from theater.regie.trajectory.timeline_layout import (
    TimelineLayout,
    TimelineSpan,
    build_timeline_layout,
)
from theater.trajectory import TrajectoryLane, TrajectoryRecord, TrajectoryStatus


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
        "trajectory-timeline--hovered",
        "trajectory-timeline--input",
        "trajectory-timeline--label",
        "trajectory-timeline--model",
        "trajectory-timeline--muted",
        "trajectory-timeline--rail",
        "trajectory-timeline--related",
        "trajectory-timeline--running",
        "trajectory-timeline--selected",
        "trajectory-timeline--theater",
        "trajectory-timeline--tools",
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
    Timeline > .trajectory-timeline--model {{ background: $accent 28%; }}
    Timeline > .trajectory-timeline--tools {{ background: $warning 26%; }}
    Timeline > .trajectory-timeline--theater {{ background: $secondary 26%; }}
    Timeline > .trajectory-timeline--error {{ background: $error; }}
    Timeline > .trajectory-timeline--running {{ background: $warning 32%; }}
    Timeline > .trajectory-timeline--muted {{ opacity: 32%; }}
    Timeline > .trajectory-timeline--hovered {{ color: $text; text-style: bold; }}
    Timeline > .trajectory-timeline--related {{ color: $text-muted; text-style: bold; }}
    Timeline > .trajectory-timeline--selected {{
        background: $accent 40%;
    }}
    """

    _LANES = tuple(TrajectoryLane)

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
        self._related_ids: frozenset[str] = frozenset()
        self._selected_id = selected_id
        self._matched_ids: frozenset[str] = frozenset()
        self._duration_mode = duration_mode
        self._layout = build_timeline_layout((), OrderMode.ORDER)
        self._span_by_id: dict[str, TimelineSpan] = {}
        self._lane_spans: dict[TrajectoryLane, tuple[TimelineSpan, ...]] = dict.fromkeys(
            self._LANES, ()
        )
        self._lane_starts: dict[TrajectoryLane, tuple[int, ...]] = dict.fromkeys(self._LANES, ())
        self._lane_max_ends: dict[TrajectoryLane, tuple[int, ...]] = dict.fromkeys(self._LANES, ())
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
    def related_ids(self) -> frozenset[str]:
        return self._related_ids

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

    def _lane_style(self, lane: TrajectoryLane) -> Style:
        return self._component(lane.value)

    def _span_style(self, record: TrajectoryRecord) -> Style:
        style = self._lane_style(record.lane)
        if record.status in {TrajectoryStatus.ERROR, TrajectoryStatus.INTERRUPTED}:
            style += self._component("error")
        elif record.status in {TrajectoryStatus.PENDING, TrajectoryStatus.RUNNING}:
            style += self._component("running")
        if record.record_id not in self._matched_ids:
            style += self._component("muted")
        if record.record_id == self._selected_id:
            style += self._component("selected")
        return style

    def _lane_row(self, y: int) -> tuple[TrajectoryLane, int] | None:
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

    def _paint_marker(
        self,
        characters: list[str],
        styles: list[Style],
        span: TimelineSpan,
        start: int,
        width: int,
        *,
        related: bool,
    ) -> None:
        visible_start = max(span.visual_start, start)
        visible_end = min(span.visual_end, start + width)
        if visible_start >= visible_end:
            return
        left = visible_start - start
        right = visible_end - start - 1
        component = self._component("related" if related else "hovered")
        marker_style = Style(color=component.color, bold=True)
        if related:
            position = (left + right) // 2
            characters[position] = TIMELINE_RELATED_GLYPH
            styles[position] += marker_style
        elif left == right:
            characters[left] = TIMELINE_HOVER_SINGLE_GLYPH
            styles[left] += marker_style
        else:
            characters[left] = TIMELINE_HOVER_LEFT_GLYPH
            characters[right] = TIMELINE_HOVER_RIGHT_GLYPH
            styles[left] += marker_style
            styles[right] += marker_style

    def _lane_strip(
        self,
        lane: TrajectoryLane,
        start: int,
        width: int,
        row: int = TIMELINE_LANE_HEIGHT // 2,
    ) -> Strip:
        characters = [" "] * width
        paints_spans = row == TIMELINE_LANE_HEIGHT // 2
        base_style = self._component("rail" if paints_spans else "track")
        styles = [base_style] * width
        spans = self._lane_spans[lane]
        first = bisect_right(self._lane_max_ends[lane], start)
        last = bisect_left(self._lane_starts[lane], start + width)
        if paints_spans:
            visible_spans = spans[first:last]
            for span in sorted(visible_spans, key=lambda item: item.width, reverse=True):
                record = self._records_by_id.get(span.record_id)
                if record is None:
                    continue
                style = self._span_style(record)
                for x in range(
                    max(0, span.visual_start - start),
                    min(width, span.visual_end - start),
                ):
                    styles[x] = style
            for record_id in self._related_ids:
                related_span = self._span_by_id.get(record_id)
                if related_span is not None and related_span.lane is lane:
                    self._paint_marker(characters, styles, related_span, start, width, related=True)
            hovered_span = self._span_by_id.get(self._hovered_id or "")
            if hovered_span is not None and hovered_span.lane is lane:
                self._paint_marker(characters, styles, hovered_span, start, width, related=False)
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
            starts = tuple(span.x for span in spans)
            furthest = 0
            max_ends: list[int] = []
            for span in spans:
                furthest = max(furthest, span.end)
                max_ends.append(furthest)
            self._lane_spans[lane] = spans
            self._lane_starts[lane] = starts
            self._lane_max_ends[lane] = tuple(max_ends)

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
        related_ids: frozenset[str] | set[str] | None = None,
        selected_id: str | None = None,
        duration_mode: bool = False,
        scroll_offset: int | None = None,
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
        self._related_ids = frozenset(
            record_id
            for record_id in related_ids or ()
            if record_id in self._records_by_id and record_id != self._hovered_id
        )
        self._duration_mode = duration_mode
        mode = OrderMode.DURATION if duration_mode else OrderMode.ORDER
        self._layout = build_timeline_layout(
            self._records,
            mode,
            minimum_width=self._available_cells(),
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
            self.scroll_x = self._scroll_offset
        if repaint:
            self.refresh()
        return self._scroll_offset

    def scroll_to_tail(self) -> int:
        return self.set_scroll_offset(self.tail_offset)

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
        spans = self._lane_spans[lane]
        index = bisect_right(self._lane_starts[lane], chart_x) - 1
        matches: list[TimelineSpan] = []
        while index >= 0:
            if self._lane_max_ends[lane][index] <= chart_x:
                break
            span = spans[index]
            if span.x <= chart_x < span.end:
                matches.append(span)
            index -= 1
        if not matches:
            return None
        span = min(matches, key=lambda item: (item.width, -item.x))
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
        related_ids: frozenset[str] | set[str] | None = None,
    ) -> None:
        record_id = record.record_id if record else None
        record_changed = record_id != self._hovered_id
        next_related = (
            frozenset(
                candidate
                for candidate in related_ids
                if candidate in self._records_by_id and candidate != record_id
            )
            if related_ids is not None
            else self._related_ids
            if not record_changed
            else frozenset()
        )
        if not record_changed and next_related == self._related_ids:
            return
        self._hovered_id = record_id
        self._related_ids = next_related
        if record_id is not None:
            self._span_index = self._span_indices[record_id]
        self.refresh()
        if notify and record_changed:
            self.post_message(TimelineSpanHovered(record_id))

    def set_hovered(
        self,
        record_id: str | None,
        *,
        related_ids: frozenset[str] | set[str] | None = None,
    ) -> None:
        self._set_hover(
            self._records_by_id.get(record_id or ""),
            notify=False,
            related_ids=related_ids,
        )

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
                related_ids=self._related_ids,
                selected_id=self._selected_id,
                duration_mode=self._duration_mode,
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
