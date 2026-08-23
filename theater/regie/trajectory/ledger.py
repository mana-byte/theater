"""Viewport-rendered ledger with one widget and bounded visible rows."""

from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text
from textual import events
from textual.message import Message
from textual.widgets import Static

from theater.regie.trajectory.constants import (
    LEDGER_DEFAULT_VIEWPORT_ROWS,
    LEDGER_OVERSCAN_ROWS,
    LEDGER_SCROLL_STEP,
)
from theater.regie.trajectory.enums import OrderMode
from theater.regie.trajectory.render import (
    group_line,
    record_line,
    sanitize_text,
    supports_duration_interval,
)
from theater.regie.trajectory.search import LedgerEntry, SearchResult
from theater.trajectory import TrajectoryRecord


class LedgerRecordHovered(Message):
    """The pointer moved over a ledger record without changing selection."""

    def __init__(self, record_id: str | None) -> None:
        super().__init__()
        self.record_id = record_id


class LedgerRecordClicked(Message):
    """A ledger record was clicked and should open the inspector."""

    def __init__(self, record_id: str | None) -> None:
        super().__init__()
        self.record_id = record_id


class LedgerGroupClicked(Message):
    """A structural turn header was clicked."""

    def __init__(self, group_id: str) -> None:
        super().__init__()
        self.group_id = group_id


class LedgerRetryClicked(Message):
    """The visible retry row was clicked."""


class Ledger(Static):
    """Render only the viewport plus a small overscan window."""

    can_focus = True

    DEFAULT_CSS = """
    Ledger {
        width: 1fr;
        height: 1fr;
        min-height: 1;
        padding: 0 1;
        overflow-x: hidden;
        overflow-y: hidden;
    }
    """

    def __init__(
        self,
        records: Sequence[TrajectoryRecord] = (),
        *,
        search_result: SearchResult | None = None,
        selected_id: str | None = None,
        hovered_id: str | None = None,
        order_mode: OrderMode = OrderMode.ORDER,
        retry_message: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__("", markup=False, **kwargs)
        self._records: dict[str, TrajectoryRecord] = {}
        self._entries: tuple[LedgerEntry, ...] = ()
        self._line_ids: tuple[str | None, ...] = ()
        self._record_indices: tuple[int | None, ...] = ()
        self._selected_id = selected_id
        self._hovered_id = hovered_id
        self._order_mode = order_mode
        self._retry_message = retry_message
        self._scroll_offset = 0
        self._viewport_height = 0
        self._rendered_line_ids: tuple[str | None, ...] = ()
        self._rendered_record_count = 0
        if search_result is not None:
            self.update_rows(
                records,
                search_result,
                selected_id=selected_id,
                hovered_id=hovered_id,
                order_mode=order_mode,
                retry_message=retry_message,
            )

    @property
    def line_ids(self) -> tuple[str | None, ...]:
        return self._line_ids

    @property
    def entries(self) -> tuple[LedgerEntry, ...]:
        return self._entries

    @property
    def viewport_rows(self) -> int:
        return self._viewport_rows()

    @property
    def rendered_line_ids(self) -> tuple[str | None, ...]:
        return self._rendered_line_ids

    @property
    def rendered_record_count(self) -> int:
        return self._rendered_record_count

    @property
    def retry_line_index(self) -> int | None:
        return len(self._entries) if self._retry_message else None

    def _viewport_rows(self) -> int:
        height = self._viewport_height or self.region.height
        return max(1, height or LEDGER_DEFAULT_VIEWPORT_ROWS)

    def _total_lines(self) -> int:
        if self._entries or self._retry_message:
            return len(self._entries) + (1 if self._retry_message else 0)
        return 1

    def _clamp_scroll(self) -> None:
        max_start = max(0, self._total_lines() - self._viewport_rows())
        self._scroll_offset = max(0, min(self._scroll_offset, max_start))

    def _line_for_record(self, record_id: str | None) -> int | None:
        if record_id is None:
            return None
        for index, entry in enumerate(self._entries):
            if entry.record_id == record_id:
                return index
        return None

    def _record_index_map(self) -> tuple[int | None, ...]:
        count = 0
        indices: list[int | None] = []
        for entry in self._entries:
            if entry.is_header:
                indices.append(None)
            else:
                indices.append(count)
                count += 1
        return tuple(indices)

    def update_rows(
        self,
        records: Sequence[TrajectoryRecord],
        search_result: SearchResult,
        *,
        selected_id: str | None = None,
        hovered_id: str | None = None,
        order_mode: OrderMode = OrderMode.ORDER,
        retry_message: str | None = None,
    ) -> None:
        old_selected = self._selected_id
        old_selected_line = self._line_for_record(old_selected)
        self._records = {record.record_id: record for record in records}
        self._entries = search_result.entries
        self._selected_id = selected_id
        self._hovered_id = hovered_id
        self._order_mode = order_mode
        self._retry_message = retry_message
        self._line_ids = tuple(entry.record_id for entry in self._entries)
        self._record_indices = self._record_index_map()
        new_selected_line = self._line_for_record(selected_id)
        if (
            old_selected == selected_id
            and old_selected_line is not None
            and new_selected_line is not None
        ):
            self._scroll_offset += new_selected_line - old_selected_line
        self._clamp_scroll()
        self._render_rows()

    def _window(self) -> tuple[int, int]:
        viewport = self._viewport_rows()
        start = max(0, self._scroll_offset - LEDGER_OVERSCAN_ROWS)
        end = min(self._total_lines(), self._scroll_offset + viewport + LEDGER_OVERSCAN_ROWS)
        return start, max(start, end)

    def _render_rows(self) -> Text:
        start, end = self._window()
        content = Text(no_wrap=True, overflow="crop")
        rendered_ids: list[str | None] = []
        rendered_records = 0
        if not self._entries and not self._retry_message:
            content.append("No loaded records match the current search or filters.")
            rendered_ids.append(None)
        else:
            for line_index in range(start, end):
                if content.plain:
                    content.append("\n")
                if self.retry_line_index == line_index:
                    retry = (
                        sanitize_text(self._retry_message or "")
                        .replace("\r", " ")
                        .replace("\n", " ")
                    )
                    content.append(f"↻ Retry: {retry}")
                    rendered_ids.append(None)
                    continue
                entry = self._entries[line_index]
                rendered_ids.append(entry.record_id)
                if entry.is_header:
                    content.append_text(
                        group_line(
                            entry.group_label,
                            collapsed=entry.collapsed,
                            depth=entry.depth,
                        )
                    )
                    continue
                record = self._records.get(entry.record_id or "")
                if record is None:
                    continue
                content.append_text(
                    record_line(
                        record,
                        self._record_indices[line_index] or 0,
                        selected=record.record_id == self._selected_id,
                        hovered=record.record_id == self._hovered_id,
                        duration_mode=(
                            self._order_mode == OrderMode.DURATION
                            and supports_duration_interval(record)
                        ),
                        depth=entry.depth,
                    )
                )
                rendered_records += 1
        self._rendered_line_ids = tuple(rendered_ids)
        self._rendered_record_count = rendered_records
        self.update(content, layout=False)
        return content

    def set_scroll_offset(self, offset: int) -> int:
        if isinstance(offset, bool):
            raise TypeError("ledger scroll offset must be an integer")
        self._scroll_offset = max(0, int(offset))
        self._clamp_scroll()
        self._render_rows()
        return self._scroll_offset

    def scroll_to_record(self, record_id: str | None) -> int:
        line = self._line_for_record(record_id)
        if line is None:
            return self._scroll_offset
        viewport = self._viewport_rows()
        if line < self._scroll_offset:
            self._scroll_offset = line
        elif line >= self._scroll_offset + viewport:
            self._scroll_offset = line - viewport + 1
        self._clamp_scroll()
        self._render_rows()
        return self._scroll_offset

    def set_hovered(self, record_id: str | None) -> None:
        self._hovered_id = record_id if record_id in self._records else None
        self._render_rows()

    def set_selected(self, record_id: str | None) -> None:
        self._selected_id = record_id
        self._render_rows()

    def _entry_at(self, y: int) -> LedgerEntry | None:
        padding = self.styles.padding.top
        padding_top = int(getattr(padding, "value", padding) or 0)
        line = y - padding_top + self._scroll_offset
        if line < 0 or line >= len(self._entries):
            return None
        return self._entries[line]

    def _line_at(self, y: int) -> int:
        padding = self.styles.padding.top
        padding_top = int(getattr(padding, "value", padding) or 0)
        return y - padding_top + self._scroll_offset

    def _hover_at(self, y: int) -> None:
        entry = self._entry_at(y)
        record_id = entry.record_id if entry and not entry.is_header else None
        if record_id == self._hovered_id:
            return
        self._hovered_id = record_id
        self.post_message(LedgerRecordHovered(record_id))

    def on_resize(self, event: events.Resize) -> None:
        self._viewport_height = max(1, event.size.height)
        self._clamp_scroll()
        self._render_rows()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        event.stop()
        self.set_scroll_offset(self._scroll_offset - LEDGER_SCROLL_STEP)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        event.stop()
        self.set_scroll_offset(self._scroll_offset + LEDGER_SCROLL_STEP)

    def on_mouse_move(self, event: events.MouseMove) -> None:
        self._hover_at(int(event.y))

    def on_leave(self, _event: events.Leave) -> None:
        self._hover_at(-1)

    def on_click(self, event: events.Click) -> None:
        line = self._line_at(int(event.y))
        if self.retry_line_index == line:
            event.stop()
            self.post_message(LedgerRetryClicked())
            return
        entry = self._entry_at(int(event.y))
        if entry is None:
            return
        event.stop()
        if entry.is_header:
            self.post_message(LedgerGroupClicked(entry.group_id))
        else:
            self._selected_id = entry.record_id
            self.post_message(LedgerRecordClicked(entry.record_id))


__all__ = [
    "Ledger",
    "LedgerGroupClicked",
    "LedgerRecordClicked",
    "LedgerRecordHovered",
    "LedgerRetryClicked",
]
