"""Fixed-top, one-cell-per-record timeline widget."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from rich.text import Text
from textual import events
from textual.message import Message
from textual.timer import Timer
from textual.widgets import Static

from theater.regie.trajectory.models import TrajectoryRecord
from theater.regie.trajectory.render import TOOLTIP_DELAY, lane_glyph, tooltip_text


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


class Timeline(Static):
    """Render timeline spans in one Static rather than one widget per record."""

    can_focus = True

    DEFAULT_CSS = """
    Timeline {
        width: 1fr;
        height: 3;
        min-height: 3;
        padding: 0 1;
        overflow-x: auto;
        overflow-y: hidden;
    }
    """

    def __init__(
        self,
        records: Sequence[TrajectoryRecord] = (),
        *,
        matched_ids: frozenset[str] | set[str] | None = None,
        tooltip_hook: Callable[[TrajectoryRecord], None] | None = None,
        **kwargs,
    ) -> None:
        super().__init__("", markup=False, **kwargs)
        self._records: tuple[TrajectoryRecord, ...] = ()
        self._span_ids: tuple[str, ...] = ()
        self._span_index = 0
        self._hovered_id: str | None = None
        self._matched_ids: frozenset[str] = frozenset()
        self._tooltip_timer: Timer | None = None
        self._tooltip_hook = tooltip_hook
        self.update_records(records, matched_ids=matched_ids)

    @property
    def records(self) -> tuple[TrajectoryRecord, ...]:
        return self._records

    @property
    def hovered_id(self) -> str | None:
        return self._hovered_id

    @property
    def span_ids(self) -> tuple[str, ...]:
        return self._span_ids

    def _render_timeline(self) -> Text:
        content = Text()
        for record in self._records:
            style = "dim" if record.record_id not in self._matched_ids else ""
            content.append(lane_glyph(record.lane), style=style)
        return content

    def update_records(
        self,
        records: Sequence[TrajectoryRecord],
        *,
        matched_ids: frozenset[str] | set[str] | None = None,
        hovered_id: str | None = None,
    ) -> None:
        self._records = tuple(records)
        self._span_ids = tuple(record.record_id for record in self._records)
        self._matched_ids = (
            frozenset(record.record_id for record in self._records)
            if matched_ids is None
            else frozenset(matched_ids)
        )
        if hovered_id in self._span_ids:
            self._hovered_id = hovered_id
        elif self._hovered_id not in self._span_ids:
            self._hovered_id = None
        if self._span_ids:
            self._span_index = min(self._span_index, len(self._span_ids) - 1)
        else:
            self._span_index = 0
        self.update(self._render_timeline(), layout=False)

    def _record_at(self, x: int) -> TrajectoryRecord | None:
        if not self._records:
            return None
        index = max(0, min(len(self._records) - 1, x - 1))
        return self._records[index]

    def _set_hover(self, record: TrajectoryRecord | None, *, immediate: bool = False) -> None:
        record_id = record.record_id if record else None
        if record_id == self._hovered_id and not immediate:
            return
        self._hovered_id = record_id
        if record_id is not None:
            self._span_index = self._span_ids.index(record_id)
        self.post_message(TimelineSpanHovered(record_id))
        self._schedule_tooltip(record, immediate=immediate)

    def _schedule_tooltip(self, record: TrajectoryRecord | None, *, immediate: bool) -> None:
        if self._tooltip_timer is not None:
            self._tooltip_timer.stop()
            self._tooltip_timer = None
        if record is None:
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
        return record.record_id

    def select_span(self, record_id: str | None) -> None:
        if record_id is None or record_id not in self._span_ids:
            return
        self._span_index = self._span_ids.index(record_id)
        self._set_hover(self._records[self._span_index], immediate=True)

    def on_mouse_move(self, event: events.MouseMove) -> None:
        self._set_hover(self._record_at(int(event.x)))

    def on_leave(self, _event: events.Leave) -> None:
        self._set_hover(None)

    def on_click(self, event: events.Click) -> None:
        record = self._record_at(event.x)
        if record is None:
            return
        event.stop()
        self._set_hover(record, immediate=True)
        self.post_message(TimelineSpanClicked(record.record_id))

    def on_unmount(self) -> None:
        if self._tooltip_timer is not None:
            self._tooltip_timer.stop()
            self._tooltip_timer = None


__all__ = [
    "Timeline",
    "TimelineSpanClicked",
    "TimelineSpanHovered",
    "TimelineTooltipRequested",
]
