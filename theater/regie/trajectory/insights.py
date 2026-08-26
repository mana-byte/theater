"""Virtualized Textual table for trajectory diagnostics."""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual import events
from textual.coordinate import Coordinate
from textual.message import Message
from textual.widgets import DataTable

from theater.constants.regie_trajectory import (
    TRAJECTORY_AUXILIARY_ROW_HEIGHT,
    TRAJECTORY_HOVERED_SPAN_ROW_HEIGHT,
    TRAJECTORY_INSIGHT_HEADER_HEIGHT,
    TRAJECTORY_SPAN_ROW_HEIGHT,
    TRAJECTORY_TABLE_CELL_PADDING,
)
from theater.regie.trajectory.analysis import TrajectoryAnalysisIndex
from theater.regie.trajectory.enums import DiagnosticView
from theater.regie.trajectory.insight_tables import (
    InsightEntry,
    InsightTableModel,
    build_insight_table,
)
from theater.regie.trajectory.render import bottom_aligned_cell
from theater.regie.trajectory.table_rows import resize_rows
from theater.trajectory import ParticipantLink

_EMPTY_KEY = "__empty__"


class InsightHighlighted(Message):
    """One insight row became the pointer or keyboard target."""

    def __init__(self, record_id: str | None) -> None:
        super().__init__()
        self.record_id = record_id


class InsightActivated(Message):
    """One insight row requested record or participant navigation."""

    def __init__(
        self,
        record_id: str | None,
        link: ParticipantLink | None = None,
    ) -> None:
        super().__init__()
        self.record_id = record_id
        self.link = link


class InsightsPanel(DataTable[Text | str]):
    """One virtualized table reused by every specialized diagnostic view."""

    can_focus = True
    COMPONENT_CLASSES: ClassVar[set[str]] = DataTable.COMPONENT_CLASSES

    DEFAULT_CSS = """
    InsightsPanel {
        width: 1fr;
        height: 1fr;
        min-height: 4;
        background: $background;
        color: $foreground;
        scrollbar-size: 0 0;
    }
    InsightsPanel > .datatable--header {
        background: $foreground 3%;
        color: $text-muted;
        text-style: bold;
    }
    InsightsPanel > .datatable--even-row { background: $foreground 3%; }
    InsightsPanel > .datatable--odd-row { background: $background; }
    InsightsPanel > .datatable--hover { background: $accent 10%; }
    InsightsPanel > .datatable--cursor { background: $accent 18%; color: $text; }
    InsightsPanel:focus > .datatable--cursor { background: $accent 28%; color: $text; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(
            show_header=True,
            show_row_labels=False,
            zebra_stripes=True,
            cursor_type="row",
            cell_padding=TRAJECTORY_TABLE_CELL_PADDING,
            header_height=TRAJECTORY_INSIGHT_HEADER_HEIGHT,
            **kwargs,
        )
        self._entries: dict[str, InsightEntry] = {}
        self._model_key: tuple[object, ...] | None = None
        self._syncing = False
        self._mouse_activation_key: str | None = None
        self._pointer_hover_key: str | None = None
        self._selected_key: str | None = None
        self._expanded_span_key: str | None = None
        self._row_heights: dict[str, int] = {}

    @property
    def insight_count(self) -> int:
        return len(self._entries)

    @property
    def record_ids(self) -> tuple[str, ...]:
        return tuple(
            entry.record_id for entry in self._entries.values() if entry.record_id is not None
        )

    @property
    def tail_record_id(self) -> str | None:
        key = next(reversed(self._entries), None)
        return self._entries[key].record_id if key is not None else None

    def is_tail_record(self, record_id: str | None) -> bool:
        key = next(reversed(self._entries), None)
        if key is None or record_id is None:
            return False
        entry = self._entries[key]
        return record_id == entry.record_id or record_id in entry.member_record_ids

    def update_analysis(
        self,
        view: DiagnosticView,
        index: TrajectoryAnalysisIndex,
        visible_ids: frozenset[str],
        *,
        selected_id: str | None,
        follow_tail: bool,
    ) -> str | None:
        model = build_insight_table(view, index, visible_ids)
        key = (view, model)
        if key != self._model_key:
            current = self._current_entry()
            current_key = (
                current.key
                if current is not None and self._entry_matches(current, selected_id)
                else None
            )
            self._model_key = key
            self._pointer_hover_key = None
            self._selected_key = None
            self._expanded_span_key = None
            self.clear(columns=True)
            for column in model.columns:
                self.add_column(column.label, key=column.key, width=column.width)
            self._entries = {entry.key: entry for entry in model.entries}
            self._row_heights = {}
            if model.entries:
                for entry in model.entries:
                    height = entry.row_height if entry.row_height is not None else model.row_height
                    self._row_heights[entry.key] = height
                    row_cells = (
                        tuple(bottom_aligned_cell(cell, height) for cell in entry.cells)
                        if height > 1
                        else entry.cells
                    )
                    self.add_row(*row_cells, key=entry.key, height=height)
            else:
                cells: list[Text | str] = [Text(model.empty_message, style="dim")]
                cells.extend("" for _ in model.columns[1:])
                self.add_row(
                    *(
                        bottom_aligned_cell(cell, TRAJECTORY_AUXILIARY_ROW_HEIGHT)
                        for cell in cells
                    ),
                    key=_EMPTY_KEY,
                    height=TRAJECTORY_AUXILIARY_ROW_HEIGHT,
                )
            if follow_tail:
                self._set_tail_cursor()
            elif current_key is not None and current_key in self._entries:
                self._set_cursor_by_key(current_key)
            else:
                self.set_selected(selected_id)
        else:
            current = self._current_entry()
            if follow_tail:
                self._set_tail_cursor()
            elif not self._entry_matches(current, selected_id):
                self.set_selected(selected_id)
        current = self._current_entry()
        return current.record_id if current is not None else None

    @staticmethod
    def _entry_matches(entry: InsightEntry | None, record_id: str | None) -> bool:
        return bool(
            entry is not None
            and record_id is not None
            and (entry.record_id == record_id or record_id in entry.member_record_ids)
        )

    def _set_cursor_by_key(self, key: str) -> None:
        for row, entry_key in enumerate(self._entries):
            if entry_key == key:
                self.move_cursor(row=row, animate=False)
                self._selected_key = key
                self._sync_expanded_span()
                return

    def _set_tail_cursor(self) -> None:
        key = next(reversed(self._entries), None)
        if key is not None:
            self._set_cursor_by_key(key)

    def _entry_for_row(self, row: int) -> InsightEntry | None:
        if not self.is_valid_row_index(row):
            return None
        key = str(self.ordered_rows[row].key.value)
        return self._entries.get(key)

    def _current_entry(self) -> InsightEntry | None:
        return self._entry_for_row(self.cursor_row)

    def _sync_expanded_span(self) -> None:
        key = self._pointer_hover_key or self._selected_key
        if self._row_heights.get(key or "") != TRAJECTORY_SPAN_ROW_HEIGHT:
            key = None
        if key == self._expanded_span_key:
            return
        heights: dict[str, int] = {}
        for candidate, height in (
            (self._expanded_span_key, TRAJECTORY_SPAN_ROW_HEIGHT),
            (key, TRAJECTORY_HOVERED_SPAN_ROW_HEIGHT),
        ):
            entry = self._entries.get(candidate or "")
            if candidate is None or entry is None:
                continue
            for column, cell in zip(self.ordered_columns, entry.cells, strict=True):
                self.update_cell(candidate, column.key, bottom_aligned_cell(cell, height))
            heights[candidate] = height
        self._expanded_span_key = key
        resize_rows(self, heights)

    def _set_pointer_hover_key(self, key: str | None) -> None:
        self._pointer_hover_key = (
            key if self._row_heights.get(key or "") == TRAJECTORY_SPAN_ROW_HEIGHT else None
        )
        self._sync_expanded_span()

    def set_selected(self, record_id: str | None) -> None:
        if record_id is None:
            return
        candidates = [
            (entry.record_id != record_id, len(entry.member_record_ids), index)
            for index, entry in enumerate(self._entries.values())
            if record_id in entry.member_record_ids or entry.record_id == record_id
        ]
        if not candidates:
            return
        row = min(candidates)[2]
        key = tuple(self._entries)[row]
        if row == self.cursor_row and key == self._selected_key:
            self._sync_expanded_span()
            return
        self._syncing = True
        self._set_cursor_by_key(key)
        self._syncing = False

    def move_row(self, delta: int) -> str | None:
        if not self._entries:
            return None
        row = max(0, min(len(self._entries) - 1, self.cursor_row + delta))
        entry = self._entry_for_row(row)
        if entry is not None:
            self._set_cursor_by_key(entry.key)
        return entry.record_id if entry is not None else None

    def _activate(self, entry: InsightEntry) -> bool:
        if entry.record_id is None and entry.link is None:
            return False
        self.post_message(InsightActivated(entry.record_id, entry.link))
        return True

    def activate_current(self) -> bool:
        entry = self._current_entry()
        if entry is None:
            return False
        return self._activate(entry)

    def watch_hover_coordinate(self, old: Coordinate, value: Coordinate) -> None:
        super().watch_hover_coordinate(old, value)
        entry = self._entry_for_row(value.row)
        pointer_key = entry.key if entry is not None else None
        if old.row == value.row and pointer_key == self._pointer_hover_key:
            return
        self._set_pointer_hover_key(pointer_key)
        self.post_message(InsightHighlighted(entry.record_id if entry is not None else None))

    def _on_leave(self, event: events.Leave) -> None:
        self._set_pointer_hover_key(None)
        super()._on_leave(event)
        self.post_message(InsightHighlighted(None))

    def on_data_table_row_highlighted(self, message: DataTable.RowHighlighted) -> None:
        if message.data_table is not self or self._syncing:
            return
        entry = self._entries.get(str(message.row_key.value))
        self._selected_key = entry.key if entry is not None else None
        self._sync_expanded_span()
        self.post_message(InsightHighlighted(entry.record_id if entry is not None else None))
        message.stop()

    def on_data_table_row_selected(self, message: DataTable.RowSelected) -> None:
        if message.data_table is not self:
            return
        key = str(message.row_key.value)
        if key == self._mouse_activation_key:
            self._mouse_activation_key = None
            message.stop()
            return
        entry = self._entries.get(key)
        if entry is not None:
            self._activate(entry)
        message.stop()

    async def _on_click(self, event: events.Click) -> None:
        if event.button != 1:
            return
        row = event.style.meta.get("row")
        column = event.style.meta.get("column")
        if not isinstance(row, int) or not self.is_valid_row_index(row):
            return
        if not isinstance(column, int) or not self.is_valid_column_index(column):
            column = 0
        self.move_cursor(row=row, column=column, animate=False)
        entry = self._entry_for_row(row)
        self._selected_key = entry.key if entry is not None else None
        self._sync_expanded_span()
        if entry is not None and self._activate(entry):
            self._mouse_activation_key = entry.key
            self.call_after_refresh(self._clear_mouse_activation, entry.key)
        event.stop()

    def _clear_mouse_activation(self, key: str) -> None:
        if self._mouse_activation_key == key:
            self._mouse_activation_key = None


__all__ = [
    "InsightActivated",
    "InsightEntry",
    "InsightHighlighted",
    "InsightTableModel",
    "InsightsPanel",
    "build_insight_table",
]
