"""Virtualized ledger rendering: one Static, no widget per trajectory record."""

from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text
from textual import events
from textual.message import Message
from textual.widgets import Static

from theater.regie.trajectory.models import OrderMode, TrajectoryRecord
from theater.regie.trajectory.render import group_line, record_line
from theater.regie.trajectory.search import LedgerEntry, SearchResult


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


class Ledger(Static):
    """Render all currently visible rows in one virtualized text surface."""

    can_focus = True

    DEFAULT_CSS = """
    Ledger {
        width: 1fr;
        height: auto;
        min-height: 1;
        padding: 0 1;
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
        self._records: dict[str, TrajectoryRecord] = {
            record.record_id: record for record in records
        }
        self._entries: tuple[LedgerEntry, ...] = ()
        self._line_ids: tuple[str | None, ...] = ()
        self._selected_id = selected_id
        self._hovered_id = hovered_id
        self._order_mode = order_mode
        self._retry_message = retry_message
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
        self._records = {record.record_id: record for record in records}
        self._entries = search_result.entries
        self._selected_id = selected_id
        self._hovered_id = hovered_id
        self._order_mode = order_mode
        self._retry_message = retry_message
        self._line_ids = tuple(entry.record_id for entry in self._entries)
        self.update(self._render_rows(), layout=False)

    def _render_rows(self) -> Text:
        content = Text()
        record_index = 0
        for line_index, entry in enumerate(self._entries):
            if line_index:
                content.append("\n")
            if entry.is_header:
                content.append_text(group_line(entry.group_label, collapsed=entry.collapsed))
                continue
            record = self._records.get(entry.record_id or "")
            if record is None:
                continue
            content.append_text(
                record_line(
                    record,
                    record_index,
                    selected=record.record_id == self._selected_id,
                    hovered=record.record_id == self._hovered_id,
                    duration_mode=self._order_mode == OrderMode.DURATION,
                )
            )
            record_index += 1
        if self._retry_message:
            if len(self._line_ids):
                content.append("\n")
            content.append(f"↻ Retry: {self._retry_message}")
        if not self._entries and not self._retry_message:
            content.append("No loaded records match the current search or filters.")
        return content

    def _entry_at(self, y: int) -> LedgerEntry | None:
        if not self._entries or y < 0 or y >= len(self._entries):
            return None
        return self._entries[y]

    def _hover_at(self, y: int) -> None:
        entry = self._entry_at(y)
        record_id = entry.record_id if entry and not entry.is_header else None
        if record_id == self._hovered_id:
            return
        self._hovered_id = record_id
        self.post_message(LedgerRecordHovered(record_id))

    def on_mouse_move(self, event: events.MouseMove) -> None:
        self._hover_at(int(event.y))

    def on_leave(self, _event: events.Leave) -> None:
        self._hover_at(-1)

    def on_click(self, event: events.Click) -> None:
        entry = self._entry_at(int(event.y))
        if entry is None:
            return
        event.stop()
        if entry.is_header:
            self.post_message(LedgerGroupClicked(entry.group_id))
        else:
            self._selected_id = entry.record_id
            self.post_message(LedgerRecordClicked(entry.record_id))


__all__ = ["Ledger", "LedgerGroupClicked", "LedgerRecordClicked", "LedgerRecordHovered"]
