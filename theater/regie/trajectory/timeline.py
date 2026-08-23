"""Fixed-top, one-cell-per-record timeline widget."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from rich.text import Text
from textual import events
from textual.message import Message
from textual.timer import Timer
from textual.widgets import Static

from theater.regie.trajectory.constants import (
    STYLE_DURATION,
    STYLE_HOVERED,
    STYLE_MATCHED,
    STYLE_SELECTED,
    STYLE_UNMATCHED,
    TIMELINE_HEIGHT,
    TIMELINE_PADDING,
    TOOLTIP_DELAY,
)
from theater.regie.trajectory.models import TrajectoryRecord
from theater.regie.trajectory.render import lane_glyph, tooltip_text


class TimelineSpanHovered(Message):
    """A pointer or keyboard moved over one timeline span."""

    def __init__(self, record_id: str | None) -> None:
        super().__init__()
        self.record_id = record_id


class TimelineSpanClicked(Message):
    """A pointer selected one timeline span."""

    def __init__(self, record_id: str | None) -> None:
        super().__init__()
        self.record_id = record_id


class TimelineTooltipRequested(Message):
    """The delayed tooltip hook fired for a span."""

    def __init__(self, record_id: str | None, text: str = "") -> None:
        super().__init__()
        self.record_id = record_id
        self.text = text


class TimelineScrolled(Message):
    """The native horizontal viewport changed."""

    def __init__(self, offset: int) -> None:
        super().__init__()
        self.offset = offset


class Timeline(Static):
    """Render one fixed-width glyph per record with native horizontal scrolling."""

    can_focus = True

    DEFAULT_CSS = f"""
    Timeline {{
        width: 1fr;
        height: {TIMELINE_HEIGHT};
        min-height: {TIMELINE_HEIGHT};
        padding: 0 {TIMELINE_PADDING};
        overflow-x: auto;
        overflow-y: hidden;
    }}
    """

    def __init__(
        self,
        records: Sequence[TrajectoryRecord] = (),
        *,
        matched_ids: frozenset[str] | set[str] | None = None,
        selected_id: str | None = None,
        duration_mode: bool = False,
        scroll_offset: int = 0,
        tooltip_hook: Callable[[TrajectoryRecord], None] | None = None,
        **kwargs,
    ) -> None:
        super().__init__("", markup=False, **kwargs)
        self._records: tuple[TrajectoryRecord, ...] = ()
        self._span_ids: tuple[str, ...] = ()
        self._span_index = 0
        self._hovered_id: str | None = None
        self._selected_id: str | None = selected_id
        self._matched_ids: frozenset[str] = frozenset()
        self._duration_mode = duration_mode
        self._scroll_offset = max(0, int(scroll_offset))
        self._viewport_width = 0
        self._tooltip_timer: Timer | None = None
        self._tooltip_hook = tooltip_hook
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
    def horizontal_offset(self) -> int:
        return self._scroll_offset

    def _render_timeline(self) -> Text:
        content = Text(no_wrap=True, overflow="crop")
        for record in self._records:
            if record.record_id == self._hovered_id:
                style = STYLE_HOVERED
            elif record.record_id == self._selected_id:
                style = STYLE_SELECTED
            elif record.record_id not in self._matched_ids:
                style = STYLE_UNMATCHED
            else:
                style = STYLE_MATCHED
            if self._duration_mode and record.record_id not in {
                self._hovered_id,
                self._selected_id,
            }:
                style = f"{style} {STYLE_DURATION}".strip()
            content.append(lane_glyph(record.lane), style=style)
        return content

    def update_records(
        self,
        records: Sequence[TrajectoryRecord],
        *,
        matched_ids: frozenset[str] | set[str] | None = None,
        hovered_id: str | None = None,
        selected_id: str | None = None,
        duration_mode: bool = False,
        scroll_offset: int | None = None,
    ) -> None:
        self._records = tuple(records)
        self._span_ids = tuple(record.record_id for record in self._records)
        self._matched_ids = (
            frozenset(record.record_id for record in self._records)
            if matched_ids is None
            else frozenset(matched_ids)
        )
        self._selected_id = selected_id
        self._duration_mode = duration_mode
        if selected_id in self._span_ids:
            self._span_index = self._span_ids.index(selected_id)
        if hovered_id in self._span_ids:
            self._hovered_id = hovered_id
        elif self._hovered_id not in self._span_ids:
            self._hovered_id = None
        if self._span_ids:
            self._span_index = min(self._span_index, len(self._span_ids) - 1)
        else:
            self._span_index = 0
        if scroll_offset is not None:
            self.set_scroll_offset(scroll_offset, repaint=False)
        self.update(self._render_timeline(), layout=False)

    def _available_cells(self) -> int:
        width = self._viewport_width or self.region.width
        return max(1, width - 2 * TIMELINE_PADDING)

    def set_scroll_offset(self, offset: int, *, repaint: bool = True) -> int:
        if isinstance(offset, bool):
            raise TypeError("timeline scroll offset must be an integer")
        self._scroll_offset = max(0, min(int(offset), max(0, len(self._records) - 1)))
        if self.is_mounted:
            self.scroll_to(x=self._scroll_offset, animate=False)
        if repaint:
            self.refresh()
        return self._scroll_offset

    def scroll_span_into_view(self, record_id: str | None) -> int:
        if record_id not in self._span_ids:
            return self._scroll_offset
        index = self._span_ids.index(record_id)
        width = self._available_cells()
        if index < self._scroll_offset:
            self.set_scroll_offset(index)
        elif index >= self._scroll_offset + width:
            self.set_scroll_offset(index - width + 1)
        return self._scroll_offset

    def _record_at(self, x: int) -> TrajectoryRecord | None:
        index = x + self._scroll_offset - TIMELINE_PADDING
        if index < 0 or index >= len(self._records):
            return None
        return self._records[index]

    def _set_hover(self, record: TrajectoryRecord | None, *, immediate: bool = False) -> None:
        record_id = record.record_id if record else None
        if record_id == self._hovered_id and not immediate:
            return
        self._hovered_id = record_id
        if record_id is not None:
            self._span_index = self._span_ids.index(record_id)
        self.update(self._render_timeline(), layout=False)
        self.post_message(TimelineSpanHovered(record_id))
        self._schedule_tooltip(record, immediate=immediate)

    def set_hovered(self, record_id: str | None) -> None:
        record = (
            self._records[self._span_ids.index(record_id)] if record_id in self._span_ids else None
        )
        self._set_hover(record, immediate=False)

    def _schedule_tooltip(self, record: TrajectoryRecord | None, *, immediate: bool) -> None:
        if self._tooltip_timer is not None:
            self._tooltip_timer.stop()
            self._tooltip_timer = None
        if record is None:
            self.post_message(TimelineTooltipRequested(None, ""))
            return
        if immediate:
            self._emit_tooltip(record)
        else:
            self._tooltip_timer = self.set_timer(TOOLTIP_DELAY, lambda: self._emit_tooltip(record))

    def _emit_tooltip(self, record: TrajectoryRecord) -> None:
        self._tooltip_timer = None
        text = tooltip_text(record)
        self.post_message(TimelineTooltipRequested(record.record_id, text))
        if self._tooltip_hook is not None:
            self._tooltip_hook(record)

    def move_span(self, delta: int) -> str | None:
        if not self._span_ids:
            return None
        self._span_index = max(0, min(len(self._span_ids) - 1, self._span_index + delta))
        record = self._records[self._span_index]
        self._set_hover(record, immediate=True)
        self.scroll_span_into_view(record.record_id)
        return record.record_id

    def select_span(self, record_id: str | None) -> None:
        if record_id is None or record_id not in self._span_ids:
            return
        self._span_index = self._span_ids.index(record_id)
        self._selected_id = record_id
        self.scroll_span_into_view(record_id)
        self._set_hover(self._records[self._span_index], immediate=True)

    def on_resize(self, event: events.Resize) -> None:
        self._viewport_width = max(1, event.size.width)
        self.scroll_span_into_view(self._selected_id or self._hovered_id)

    def on_mouse_move(self, event: events.MouseMove) -> None:
        self._set_hover(self._record_at(int(event.x)))

    def on_leave(self, _event: events.Leave) -> None:
        self._set_hover(None)

    def on_click(self, event: events.Click) -> None:
        record = self._record_at(int(event.x))
        if record is None:
            return
        event.stop()
        self._set_hover(record, immediate=True)
        self._selected_id = record.record_id
        self.update(self._render_timeline(), layout=False)
        self.post_message(TimelineSpanClicked(record.record_id))

    def on_mouse_scroll_left(self, event: events.MouseScrollLeft) -> None:
        event.stop()
        self.set_scroll_offset(self._scroll_offset - 1)

    def on_mouse_scroll_right(self, event: events.MouseScrollRight) -> None:
        event.stop()
        self.set_scroll_offset(self._scroll_offset + 1)

    def watch_scroll_x(self, _old_value: float, new_value: float) -> None:
        self._scroll_offset = max(0, int(new_value))
        if self.is_mounted:
            self.post_message(TimelineScrolled(self._scroll_offset))

    def on_unmount(self) -> None:
        if self._tooltip_timer is not None:
            self._tooltip_timer.stop()
            self._tooltip_timer = None


__all__ = [
    "Timeline",
    "TimelineScrolled",
    "TimelineSpanClicked",
    "TimelineSpanHovered",
    "TimelineTooltipRequested",
]
