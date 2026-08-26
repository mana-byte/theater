"""Structured virtualized trajectory event ledger."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text
from textual import events
from textual.coordinate import Coordinate
from textual.message import Message
from textual.widgets import DataTable

from theater.constants.regie_trajectory import (
    LEDGER_COMPACT_WIDTH,
    LEDGER_DEFAULT_VIEWPORT_ROWS,
    LEDGER_DURATION_COLUMN_WIDTH,
    LEDGER_HEADER_HEIGHT,
    LEDGER_MIN_SUMMARY_WIDTH,
    LEDGER_OVERSCAN_ROWS,
    LEDGER_SCROLLBAR_WIDTH,
    LEDGER_STATUS_COLUMN_WIDTH,
    TRAJECTORY_AUXILIARY_ROW_HEIGHT,
    TRAJECTORY_HOVERED_SPAN_ROW_HEIGHT,
    TRAJECTORY_SPAN_ROW_HEIGHT,
    TRAJECTORY_TABLE_CELL_PADDING,
)
from theater.regie.trajectory.enums import OrderMode
from theater.regie.trajectory.render.ledger import (
    COLUMN_DURATION as LEDGER_COLUMN_DURATION,
)
from theater.regie.trajectory.render.ledger import (
    COLUMN_EVENT as LEDGER_COLUMN_EVENT,
)
from theater.regie.trajectory.render.ledger import (
    COLUMN_LABELS as LEDGER_COLUMN_LABELS,
)
from theater.regie.trajectory.render.ledger import (
    COLUMN_POSITION as LEDGER_COLUMN_POSITION,
)
from theater.regie.trajectory.render.ledger import (
    COLUMN_SOURCE as LEDGER_COLUMN_SOURCE,
)
from theater.regie.trajectory.render.ledger import (
    COLUMN_STATUS as LEDGER_COLUMN_STATUS,
)
from theater.regie.trajectory.render.ledger import (
    COLUMN_SUMMARY as LEDGER_COLUMN_SUMMARY,
)
from theater.regie.trajectory.render.ledger import (
    LedgerRowValues,
    LedgerStylePalette,
    empty_cells,
    empty_values,
    entry_cells,
    entry_values,
    history_cells,
    history_values,
    retry_cells,
    retry_values,
)
from theater.regie.trajectory.render.ledger import (
    status_style as ledger_status_style,
)
from theater.regie.trajectory.render.records import bottom_aligned_cell
from theater.regie.trajectory.search import LedgerEntry, SearchResult
from theater.regie.trajectory.widgets.ledger_viewport import (
    max_scroll_row,
    render_slice,
    row_at_offset,
    row_offsets,
    target_scroll_position,
)
from theater.regie.trajectory.widgets.rows import resize_rows
from theater.trajectory import (
    TrajectoryRecord,
    TrajectoryRequest,
    TrajectoryStatus,
    TrajectoryToolOperation,
)


class LedgerRecordHovered(Message):
    """Pointer moved over a ledger record without changing selection."""

    def __init__(self, record_id: str | None) -> None:
        super().__init__()
        self.record_id = record_id


class LedgerRecordClicked(Message):
    """A ledger record was activated."""

    def __init__(self, record_id: str | None) -> None:
        super().__init__()
        self.record_id = record_id


class LedgerRetryClicked(Message):
    """The visible retry row was activated."""


class LedgerOlderClicked(Message):
    """The visible earlier-history row was activated."""


class Ledger(DataTable[Text | str]):
    """Render records as native table rows with content-sized columns."""

    can_focus = True
    COLUMN_POSITION = LEDGER_COLUMN_POSITION
    COLUMN_EVENT = LEDGER_COLUMN_EVENT
    COLUMN_SOURCE = LEDGER_COLUMN_SOURCE
    COLUMN_SUMMARY = LEDGER_COLUMN_SUMMARY
    COLUMN_STATUS = LEDGER_COLUMN_STATUS
    COLUMN_DURATION = LEDGER_COLUMN_DURATION
    EMPTY_KEY = "__empty__"
    OLDER_KEY = "__older__"
    RETRY_KEY = "__retry__"
    GROUP_PREFIX = "group:"
    REQUEST_PREFIX = "request:"
    RECORD_PREFIX = "record:"
    TOOL_PREFIX = "tool:"
    COLUMN_LABELS: ClassVar[dict[str, str]] = dict(LEDGER_COLUMN_LABELS)

    COMPONENT_CLASSES: ClassVar[set[str]] = DataTable.COMPONENT_CLASSES | {
        "trajectory-ledger--accent",
        "trajectory-ledger--error",
        "trajectory-ledger--input",
        "trajectory-ledger--model",
        "trajectory-ledger--muted",
        "trajectory-ledger--retry",
        "trajectory-ledger--request",
        "trajectory-ledger--theater",
        "trajectory-ledger--tools",
        "trajectory-ledger--warning",
    }

    DEFAULT_CSS = """
    Ledger {
        width: 1fr;
        height: 1fr;
        min-height: 4;
        background: $background;
        color: $foreground;
        scrollbar-size: 0 0;
    }
    Ledger > .datatable--header {
        background: $foreground 3%;
        color: $text-muted;
        text-style: bold;
    }
    Ledger > .datatable--even-row {
        background: $foreground 3%;
    }
    Ledger > .datatable--odd-row {
        background: $background;
    }
    Ledger > .datatable--hover {
        background: $accent 10%;
    }
    Ledger > .datatable--cursor {
        background: $accent 20%;
        color: $text;
        text-style: bold;
    }
    Ledger:focus > .datatable--cursor {
        background: $accent 30%;
        color: $text;
        text-style: bold;
    }
    Ledger > .datatable--fixed {
        background: transparent;
    }
    Ledger > .trajectory-ledger--input {
        color: $primary;
    }
    Ledger > .trajectory-ledger--model {
        color: $accent;
    }
    Ledger > .trajectory-ledger--tools {
        color: $warning;
    }
    Ledger > .trajectory-ledger--theater {
        color: $secondary;
    }
    Ledger > .trajectory-ledger--muted {
        color: $text-muted;
    }
    Ledger > .trajectory-ledger--warning {
        color: $warning;
    }
    Ledger > .trajectory-ledger--error {
        color: $error;
    }
    Ledger > .trajectory-ledger--accent {
        color: $accent;
        text-style: dim;
    }
    Ledger > .trajectory-ledger--retry {
        color: $text;
        background: $accent 20%;
        text-style: bold;
    }
    Ledger > .trajectory-ledger--request {
        color: $accent;
        text-style: dim;
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
        has_older: bool = False,
        loading_older: bool = False,
        retry_message: str | None = None,
        position_offset: int = 0,
        **kwargs,
    ) -> None:
        super().__init__(
            show_header=True,
            show_row_labels=False,
            fixed_columns=2,
            zebra_stripes=True,
            cursor_type="row",
            cell_padding=TRAJECTORY_TABLE_CELL_PADDING,
            header_height=LEDGER_HEADER_HEIGHT,
            **kwargs,
        )
        self._records: dict[str, TrajectoryRecord] = {}
        self._requests: dict[str, TrajectoryRequest] = {}
        self._tools: dict[str, TrajectoryToolOperation] = {}
        self._entries: tuple[LedgerEntry, ...] = ()
        self._entry_indices: dict[str, int] = {}
        self._line_ids: tuple[str | None, ...] = ()
        self._record_indices: tuple[int | None, ...] = ()
        self._row_entries: dict[str, LedgerEntry | str] = {}
        self._selected_id = selected_id
        self._hovered_id = hovered_id
        self._order_mode = order_mode
        self._has_older = has_older
        self._loading_older = loading_older
        self._retry_message = retry_message
        self._position_offset = max(0, int(position_offset))
        self._scroll_offset = 0
        self._viewport_height = 0
        self._rendered_line_ids: tuple[str | None, ...] = ()
        self._rendered_record_count = 0
        self._row_starts: tuple[int, ...] = ()
        self._rows_height = 0
        self._structure: tuple[object, ...] = ()
        self._revisions: dict[str, int] = {}
        self._rendered_requests: dict[str, TrajectoryRequest] = {}
        self._rendered_tools: dict[str, TrajectoryToolOperation] = {}
        self._summary_width = 32
        self._column_widths = {
            key: self._text_width(label) for key, label in self.COLUMN_LABELS.items()
        }
        self._compact_columns = False
        self._building = False
        self._syncing_cursor = False
        self._mouse_activation_key: str | None = None
        self._pointer_hover_key: str | None = None
        self._expanded_span_key: str | None = None
        if search_result is not None:
            self.update_rows(
                records,
                search_result,
                selected_id=selected_id,
                hovered_id=hovered_id,
                order_mode=order_mode,
                has_older=has_older,
                loading_older=loading_older,
                retry_message=retry_message,
                position_offset=position_offset,
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
        self._update_render_window()
        return self._rendered_line_ids

    @property
    def rendered_record_count(self) -> int:
        self._update_render_window()
        return self._rendered_record_count

    @property
    def retry_line_index(self) -> int | None:
        if not self._retry_message:
            return None
        return self._row_prefix + len(self._entries)

    @property
    def _row_prefix(self) -> int:
        return int(self._has_older)

    def _viewport_rows(self) -> int:
        if self._viewport_height:
            return self._viewport_height
        height = self.region.height - self.header_height
        return (
            max(1, height // TRAJECTORY_SPAN_ROW_HEIGHT) if height else LEDGER_DEFAULT_VIEWPORT_ROWS
        )

    def _viewport_content_height(self) -> int:
        if self._viewport_height:
            return self._viewport_height * TRAJECTORY_SPAN_ROW_HEIGHT
        return max(1, self.region.height - self.header_height)

    def _row_key(self, entry: LedgerEntry) -> str:
        if entry.is_request_header:
            return f"{self.REQUEST_PREFIX}{entry.request_id}"
        if entry.is_group_header:
            return f"{self.GROUP_PREFIX}{entry.group_id}"
        if entry.is_tool_operation:
            return f"{self.TOOL_PREFIX}{entry.tool_operation_id}"
        return f"{self.RECORD_PREFIX}{entry.record_id}"

    def _entry_for_key(self, key: object) -> LedgerEntry | str | None:
        value = getattr(key, "value", key)
        return self._row_entries.get(str(value))

    def _entry_index_for_record(self, record_id: str | None) -> int | None:
        if record_id is None:
            return None
        return self._entry_indices.get(record_id)

    def _row_id_for_record(self, record_id: str | None) -> str | None:
        index = self._entry_index_for_record(record_id)
        return self._entries[index].record_id if index is not None else None

    def _row_index_for_record(self, record_id: str | None) -> int | None:
        index = self._entry_index_for_record(record_id)
        if index is None:
            return None
        return self._row_prefix + index

    def _record_index_map(self) -> tuple[int | None, ...]:
        count = self._position_offset
        indices: list[int | None] = []
        for entry in self._entries:
            if entry.is_header:
                indices.append(None)
            else:
                indices.append(count)
                count += 1
        return tuple(indices)

    @property
    def _column_keys(self) -> tuple[str, ...]:
        if self._compact_columns:
            return (
                self.COLUMN_POSITION,
                self.COLUMN_EVENT,
                self.COLUMN_SUMMARY,
                self.COLUMN_STATUS,
            )
        return (
            self.COLUMN_POSITION,
            self.COLUMN_EVENT,
            self.COLUMN_SOURCE,
            self.COLUMN_SUMMARY,
            self.COLUMN_STATUS,
            self.COLUMN_DURATION,
        )

    @staticmethod
    def _text_width(value: str) -> int:
        return max((cell_len(line) for line in value.splitlines()), default=0)

    def _fixed_column_width(self) -> int:
        return sum(
            self._column_widths[column] + 2 * self.cell_padding
            for column in self._column_keys
            if column != self.COLUMN_SUMMARY
        )

    def _columns_width(self, width: int | None = None) -> int:
        available = width or self.region.width or 100
        return max(
            LEDGER_MIN_SUMMARY_WIDTH,
            available - self._fixed_column_width() - 2 * self.cell_padding - LEDGER_SCROLLBAR_WIDTH,
        )

    def _ensure_columns(self) -> None:
        if len(self.columns):
            return
        self.add_column(
            Text(f"\n{self.COLUMN_LABELS[self.COLUMN_POSITION]}"),
            width=self._column_widths[self.COLUMN_POSITION],
            key=self.COLUMN_POSITION,
        )
        self.add_column(
            Text(f"\n{self.COLUMN_LABELS[self.COLUMN_EVENT]}"),
            width=self._column_widths[self.COLUMN_EVENT],
            key=self.COLUMN_EVENT,
        )
        if not self._compact_columns:
            self.add_column(
                Text(f"\n{self.COLUMN_LABELS[self.COLUMN_SOURCE]}"),
                width=self._column_widths[self.COLUMN_SOURCE],
                key=self.COLUMN_SOURCE,
            )
        self.add_column(
            Text(f"\n{self.COLUMN_LABELS[self.COLUMN_SUMMARY]}"),
            width=self._summary_width,
            key=self.COLUMN_SUMMARY,
        )
        self.add_column(
            Text(f"\n{self.COLUMN_LABELS[self.COLUMN_STATUS]}"),
            width=self._column_widths[self.COLUMN_STATUS],
            key=self.COLUMN_STATUS,
        )
        if not self._compact_columns:
            self.add_column(
                Text(f"\n{self.COLUMN_LABELS[self.COLUMN_DURATION]}"),
                width=self._column_widths[self.COLUMN_DURATION],
                key=self.COLUMN_DURATION,
            )

    def _component(self, name: str) -> Style:
        return self.get_component_rich_style(f"trajectory-ledger--{name}", partial=True)

    def _style_palette(self) -> LedgerStylePalette:
        return LedgerStylePalette(
            input=self._component("input"),
            model=self._component("model"),
            tools=self._component("tools"),
            theater=self._component("theater"),
            muted=self._component("muted"),
            warning=self._component("warning"),
            error=self._component("error"),
            accent=self._component("accent"),
            retry=self._component("retry"),
            request=self._component("request"),
        )

    def _status_style(self, status: TrajectoryStatus) -> Style:
        return ledger_status_style(status, self._style_palette())

    def _entry_context(
        self, entry: LedgerEntry
    ) -> tuple[TrajectoryRecord | None, TrajectoryRequest | None, TrajectoryToolOperation | None]:
        return (
            self._records.get(entry.record_id or ""),
            self._requests.get(entry.request_id or ""),
            self._tools.get(entry.tool_operation_id or ""),
        )

    def _entry_values(
        self,
        entry: LedgerEntry,
        line_index: int,
        *,
        record: TrajectoryRecord | None = None,
        request: TrajectoryRequest | None = None,
        tool: TrajectoryToolOperation | None = None,
    ) -> LedgerRowValues | None:
        if record is None and request is None and tool is None:
            record, request, tool = self._entry_context(entry)
        return entry_values(
            entry,
            record=record,
            request=request,
            tool=tool,
            index=self._record_indices[line_index] or 0,
            record_hovered=record is not None and record.record_id == self._hovered_id,
            entry_hovered=entry.record_id == self._row_id_for_record(self._hovered_id),
            compact=self._compact_columns,
        )

    def _entry_cells(
        self, entry: LedgerEntry, line_index: int, palette: LedgerStylePalette
    ) -> Mapping[str, Text | str] | None:
        record, request, tool = self._entry_context(entry)
        values = self._entry_values(
            entry,
            line_index,
            record=record,
            request=request,
            tool=tool,
        )
        if values is None:
            return None
        return entry_cells(
            entry,
            values,
            record=record,
            request=request,
            tool=tool,
            palette=palette,
            record_hovered=record is not None and record.record_id == self._hovered_id,
            entry_hovered=entry.record_id == self._row_id_for_record(self._hovered_id),
            duration_mode=self._order_mode is OrderMode.DURATION,
        )

    def _measure_column_widths(self) -> dict[str, int]:
        widths = {key: self._text_width(label) for key, label in self.COLUMN_LABELS.items()}

        def include(values: Mapping[str, str]) -> None:
            for key, value in values.items():
                widths[key] = max(widths[key], self._text_width(value))

        if self._has_older:
            include(history_values(loading=self._loading_older))
        if not self._entries and not self._retry_message and not self._has_older:
            include(empty_values())
        for line_index, entry in enumerate(self._entries):
            values = self._entry_values(entry, line_index)
            if values is not None:
                include(values)
        if self._retry_message:
            include(retry_values(self._retry_message))
        if any(entry.is_tool_operation for entry in self._entries):
            widths[self.COLUMN_STATUS] = LEDGER_STATUS_COLUMN_WIDTH
            widths[self.COLUMN_DURATION] = LEDGER_DURATION_COLUMN_WIDTH
        return widths

    @staticmethod
    def _bottom_cell(value: Text | str, height: int = TRAJECTORY_SPAN_ROW_HEIGHT) -> Text:
        return bottom_aligned_cell(value, height)

    def _add_cells(
        self,
        cells: Mapping[str, Text | str],
        *,
        key: str,
        height: int = TRAJECTORY_SPAN_ROW_HEIGHT,
    ) -> None:
        self.add_row(
            *(self._bottom_cell(cells.get(column, ""), height) for column in self._column_keys),
            height=height,
            key=key,
        )

    def _structure_key(self) -> tuple[object, ...]:
        return (
            self._order_mode,
            self._compact_columns,
            tuple(
                (key, self._column_widths[key])
                for key in self._column_keys
                if key != self.COLUMN_SUMMARY
            ),
            self._summary_width,
            self._position_offset,
            self._has_older,
            self._loading_older,
            self._retry_message,
            tuple(
                (
                    entry.group_id,
                    entry.record_id,
                    entry.request_id,
                    entry.tool_operation_id,
                    entry.depth,
                    entry.group_kind,
                )
                for entry in self._entries
            ),
        )

    def _populate_rows(self, palette: LedgerStylePalette) -> None:
        self._ensure_columns()
        self._row_entries.clear()
        if self._has_older:
            loading = self._loading_older
            values = history_values(loading=loading)
            self._add_cells(
                history_cells(values, palette, loading=loading),
                key=self.OLDER_KEY,
                height=TRAJECTORY_AUXILIARY_ROW_HEIGHT,
            )
            self._row_entries[self.OLDER_KEY] = self.OLDER_KEY
        if not self._entries and not self._retry_message and not self._has_older:
            values = empty_values()
            self._add_cells(
                empty_cells(values),
                key=self.EMPTY_KEY,
                height=TRAJECTORY_AUXILIARY_ROW_HEIGHT,
            )
            self._row_entries[self.EMPTY_KEY] = self.EMPTY_KEY
            return
        for line_index, entry in enumerate(self._entries):
            key = self._row_key(entry)
            self._row_entries[key] = entry
            cells = self._entry_cells(entry, line_index, palette)
            if cells is None:
                continue
            self._add_cells(
                cells,
                key=key,
                height=(
                    TRAJECTORY_AUXILIARY_ROW_HEIGHT
                    if entry.is_header
                    else TRAJECTORY_SPAN_ROW_HEIGHT
                ),
            )
        if self._retry_message:
            values = retry_values(self._retry_message)
            self._add_cells(
                retry_cells(values, palette),
                key=self.RETRY_KEY,
                height=TRAJECTORY_AUXILIARY_ROW_HEIGHT,
            )
            self._row_entries[self.RETRY_KEY] = self.RETRY_KEY

    def _rebuild(self, *, preserve_scroll: bool = True) -> None:
        previous_scroll = self._scroll_offset if preserve_scroll else 0
        palette = self._style_palette()
        self._pointer_hover_key = None
        self._expanded_span_key = None
        self._building = True
        try:
            self.clear(columns=True)
            self._ensure_columns()
            self._populate_rows(palette)
            self._revisions = {
                record_id: record.revision for record_id, record in self._records.items()
            }
            self._rendered_requests = {
                entry.request_id: self._requests[entry.request_id]
                for entry in self._entries
                if entry.is_request_header and entry.request_id in self._requests
            }
            self._rendered_tools = dict(self._tools)
            self._update_row_starts()
            self._structure = self._structure_key()
        finally:
            self._building = False
        self.set_scroll_offset(previous_scroll)
        self._sync_selection(palette)

    def _update_row_starts(self) -> None:
        offsets = row_offsets(tuple(row.height for row in self.ordered_rows))
        self._row_starts = offsets.starts
        self._rows_height = offsets.content_height

    def _refresh_changed_records(self, palette: LedgerStylePalette) -> None:
        for line_index, entry in enumerate(self._entries):
            if entry.is_header or entry.is_tool_operation or entry.record_id is None:
                continue
            record = self._records.get(entry.record_id)
            if record is None or self._revisions.get(entry.record_id) == record.revision:
                continue
            key = self._row_key(entry)
            cells = self._entry_cells(entry, line_index, palette)
            if cells is None:
                continue
            height = self._span_row_height(key)
            for column in self._column_keys:
                self.update_cell(key, column, self._bottom_cell(cells[column], height))
            self._revisions[entry.record_id] = record.revision

    def _refresh_changed_requests(self, palette: LedgerStylePalette) -> None:
        for line_index, entry in enumerate(self._entries):
            if not entry.is_request_header or entry.request_id is None:
                continue
            request = self._requests.get(entry.request_id)
            if request is None or self._rendered_requests.get(entry.request_id) == request:
                continue
            key = self._row_key(entry)
            cells = self._entry_cells(entry, line_index, palette)
            if cells is None:
                continue
            for column in self._column_keys:
                self.update_cell(
                    key,
                    column,
                    self._bottom_cell(cells[column], TRAJECTORY_AUXILIARY_ROW_HEIGHT),
                )
            self._rendered_requests[entry.request_id] = request

    def _refresh_changed_tools(self, palette: LedgerStylePalette) -> None:
        for line_index, entry in enumerate(self._entries):
            if not entry.is_tool_operation or entry.tool_operation_id is None:
                continue
            tool = self._tools.get(entry.tool_operation_id)
            if tool is None or self._rendered_tools.get(entry.tool_operation_id) == tool:
                continue
            cells = self._entry_cells(entry, line_index, palette)
            if cells is None:
                continue
            key = self._row_key(entry)
            height = self._span_row_height(key)
            for column in self._column_keys:
                self.update_cell(key, column, self._bottom_cell(cells[column], height))
            self._rendered_tools[entry.tool_operation_id] = tool

    def _span_row_height(self, key: str) -> int:
        return (
            TRAJECTORY_HOVERED_SPAN_ROW_HEIGHT
            if key == self._expanded_span_key
            else TRAJECTORY_SPAN_ROW_HEIGHT
        )

    def _actual_row_key(self, record_id: str | None) -> str | None:
        index = self._entry_index_for_record(record_id)
        if index is None:
            return None
        entry = self._entries[index]
        return None if entry.is_header else self._row_key(entry)

    def _sync_expanded_span(self, palette: LedgerStylePalette | None = None) -> None:
        key = self._pointer_hover_key or self._actual_row_key(self._selected_id)
        entry = self._row_entries.get(key or "")
        if not isinstance(entry, LedgerEntry) or entry.is_header:
            key = None
        if key == self._expanded_span_key:
            return
        if palette is None:
            palette = self._style_palette()
        heights: dict[str, int] = {}
        for candidate, height in (
            (self._expanded_span_key, TRAJECTORY_SPAN_ROW_HEIGHT),
            (key, TRAJECTORY_HOVERED_SPAN_ROW_HEIGHT),
        ):
            row_entry = self._row_entries.get(candidate or "")
            if candidate is None or not isinstance(row_entry, LedgerEntry):
                continue
            line_index = self._entry_indices.get(row_entry.record_id or "")
            if line_index is None:
                continue
            cells = self._entry_cells(row_entry, line_index, palette)
            if cells is None:
                continue
            for column in self._column_keys:
                self.update_cell(candidate, column, self._bottom_cell(cells[column], height))
            heights[candidate] = height
        self._expanded_span_key = key
        if resize_rows(self, heights):
            self._update_row_starts()
            self._update_render_window()

    def _set_pointer_hover_key(self, key: str | None) -> None:
        entry = self._row_entries.get(key or "")
        self._pointer_hover_key = (
            key if isinstance(entry, LedgerEntry) and not entry.is_header else None
        )
        self._sync_expanded_span()

    def _set_content(
        self,
        records: Sequence[TrajectoryRecord],
        search_result: SearchResult,
    ) -> None:
        self._records = {record.record_id: record for record in records}
        self._requests = dict(search_result.requests)
        self._tools = dict(search_result.tools)
        self._entries = search_result.entries
        self._entry_indices = {
            entry.record_id: index
            for index, entry in enumerate(self._entries)
            if entry.record_id is not None
        }
        for record_id, row_id in search_result.row_id_by_record_id.items():
            if row_id in self._entry_indices:
                self._entry_indices[record_id] = self._entry_indices[row_id]

    def update_rows(
        self,
        records: Sequence[TrajectoryRecord],
        search_result: SearchResult,
        *,
        selected_id: str | None = None,
        hovered_id: str | None = None,
        order_mode: OrderMode = OrderMode.ORDER,
        has_older: bool = False,
        loading_older: bool = False,
        retry_message: str | None = None,
        position_offset: int = 0,
    ) -> None:
        old_selected_line = self._row_index_for_record(self._selected_id)
        old_selected = self._selected_id
        self._set_content(records, search_result)
        self._selected_id = selected_id
        self._hovered_id = hovered_id
        self._order_mode = order_mode
        self._has_older = has_older
        self._loading_older = loading_older
        self._retry_message = retry_message
        self._position_offset = max(0, int(position_offset))
        self._line_ids = tuple(entry.record_id for entry in self._entries)
        self._record_indices = self._record_index_map()
        self._column_widths = self._measure_column_widths()
        self._summary_width = self._columns_width()
        new_selected_line = self._row_index_for_record(selected_id)
        if (
            old_selected == selected_id
            and old_selected_line is not None
            and new_selected_line is not None
        ):
            self._scroll_offset += new_selected_line - old_selected_line
        structure = self._structure_key()
        if structure != self._structure or not len(self.columns):
            self._rebuild()
        else:
            palette = self._style_palette()
            self._refresh_changed_records(palette)
            self._refresh_changed_requests(palette)
            self._refresh_changed_tools(palette)
            self._sync_selection(palette)
            self.set_scroll_offset(self._scroll_offset)
        self._update_render_window()

    def _sync_selection(self, palette: LedgerStylePalette | None = None) -> None:
        line = self._row_index_for_record(self._selected_id)
        if line is not None and self.row_count:
            self._syncing_cursor = True
            try:
                self.move_cursor(row=line, column=0, animate=False, scroll=False)
            finally:
                self._syncing_cursor = False
        self._sync_expanded_span(palette)

    def _max_scroll_row(self) -> int:
        return max_scroll_row(
            self._row_starts,
            self._rows_height,
            viewport_height=self._viewport_content_height(),
            row_count=max(1, self._row_prefix + len(self._entries) + bool(self._retry_message)),
            viewport_rows=self._viewport_rows(),
        )

    def _clamp_scroll(self) -> None:
        self._scroll_offset = max(0, min(self._scroll_offset, self._max_scroll_row()))

    def _update_render_window(self) -> None:
        self._clamp_scroll()
        window = render_slice(
            self._scroll_offset,
            row_count=len(self.ordered_rows),
            viewport_rows=self._viewport_rows(),
            overscan_rows=LEDGER_OVERSCAN_ROWS,
        )
        visible = [
            self._row_entries.get(str(row.key.value))
            for row in self.ordered_rows[window.start : window.end]
        ]
        self._rendered_line_ids = tuple(
            entry.record_id if isinstance(entry, LedgerEntry) else None for entry in visible
        )
        self._rendered_record_count = sum(
            isinstance(entry, LedgerEntry) and not entry.is_header for entry in visible
        )

    def set_scroll_offset(self, offset: int) -> int:
        if isinstance(offset, bool):
            raise TypeError("ledger scroll offset must be an integer")
        self._scroll_offset = max(0, int(offset))
        self._clamp_scroll()
        if self.is_mounted:
            scroll_y = (
                self._row_starts[self._scroll_offset]
                if self._scroll_offset < len(self._row_starts)
                else 0
            )
            self.scroll_to(
                y=scroll_y,
                animate=False,
                force=True,
            )
        self._update_render_window()
        return self._scroll_offset

    def scroll_to_record(self, record_id: str | None) -> int:
        record_id = self._row_id_for_record(record_id)
        line = self._row_index_for_record(record_id)
        if line is None:
            return self._scroll_offset
        if line >= len(self._row_starts):
            return self._scroll_offset
        target = target_scroll_position(
            self._row_starts,
            tuple(row.height for row in self.ordered_rows),
            row=line,
            current_y=int(self.scroll_y),
            viewport_height=self._viewport_content_height(),
            content_height=self._rows_height,
        )
        if target.changed:
            self._scroll_offset = target.row
            self.scroll_to(y=target.y, animate=False, force=True)
            self._update_render_window()
        return self._scroll_offset

    def set_hovered(self, record_id: str | None) -> None:
        record_id = record_id if record_id in self._records else None
        if record_id == self._hovered_id:
            return
        previous = self._hovered_id
        self._hovered_id = record_id
        palette = self._style_palette()
        for candidate in (previous, record_id):
            line_index = self._entry_index_for_record(candidate)
            if line_index is None or candidate not in self._records:
                continue
            entry = self._entries[line_index]
            cells = self._entry_cells(entry, line_index, palette)
            if cells is None:
                continue
            key = self._row_key(entry)
            height = self._span_row_height(key)
            self.update_cell(
                key,
                self.COLUMN_POSITION,
                self._bottom_cell(cells[self.COLUMN_POSITION], height),
            )
            self.update_cell(
                key,
                self.COLUMN_SUMMARY,
                self._bottom_cell(cells[self.COLUMN_SUMMARY], height),
            )

    def set_selected(self, record_id: str | None) -> None:
        index = self._entry_index_for_record(record_id)
        if index is not None:
            record_id = self._entries[index].record_id
        if record_id == self._selected_id:
            self._sync_expanded_span()
            return
        self._selected_id = record_id
        self._sync_selection()

    def on_resize(self, event: events.Resize) -> None:
        self._viewport_height = max(
            1,
            (event.size.height - self.header_height) // TRAJECTORY_SPAN_ROW_HEIGHT,
        )
        compact_columns = event.size.width < LEDGER_COMPACT_WIDTH
        columns_changed = compact_columns != self._compact_columns
        if columns_changed:
            self._compact_columns = compact_columns
        if columns_changed:
            self._column_widths = self._measure_column_widths()
        summary_width = self._columns_width(event.size.width)
        if summary_width != self._summary_width or columns_changed:
            self._summary_width = summary_width
            if len(self.columns):
                self._rebuild()
        self._update_render_window()

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        self._scroll_offset = row_at_offset(self._row_starts, int(new_value))
        self._update_render_window()

    def watch_hover_coordinate(self, old: Coordinate, value: Coordinate) -> None:
        super().watch_hover_coordinate(old, value)
        if self._building or not self._show_hover_cursor:
            return
        if not self.is_valid_coordinate(value):
            self._set_pointer_hover_key(None)
            self.post_message(LedgerRecordHovered(None))
            return
        key = self.coordinate_to_cell_key(value).row_key
        entry = self._entry_for_key(key)
        record_id: str | None
        record_id = (
            entry.record_id if isinstance(entry, LedgerEntry) and not entry.is_header else None
        )
        pointer_key = str(key.value) if record_id is not None else None
        if old.row == value.row and pointer_key == self._pointer_hover_key:
            return
        self._set_pointer_hover_key(pointer_key)
        self.post_message(LedgerRecordHovered(record_id))

    def _on_leave(self, event: events.Leave) -> None:
        self._set_pointer_hover_key(None)
        super()._on_leave(event)
        self.post_message(LedgerRecordHovered(None))

    def on_data_table_row_selected(self, message: DataTable.RowSelected) -> None:
        if message.data_table is not self:
            return
        key = str(message.row_key.value)
        if key == self._mouse_activation_key:
            self._mouse_activation_key = None
            message.stop()
            return
        self._activate_entry(self._entry_for_key(message.row_key))
        message.stop()

    def _activate_entry(self, entry: LedgerEntry | str | None) -> None:
        if entry == self.OLDER_KEY:
            if not self._loading_older:
                self.post_message(LedgerOlderClicked())
        elif entry == self.RETRY_KEY:
            self.post_message(LedgerRetryClicked())
        elif isinstance(entry, LedgerEntry) and entry.is_header:
            self._sync_selection()
        elif isinstance(entry, LedgerEntry):
            self._selected_id = entry.record_id
            self.post_message(LedgerRecordClicked(entry.record_id))

    async def _on_click(self, event: events.Click) -> None:
        if event.button != 1:
            return
        meta = event.style.meta
        row = meta.get("row")
        column = meta.get("column")
        if not isinstance(row, int) or not self.is_valid_row_index(row):
            return
        if not isinstance(column, int) or not self.is_valid_column_index(column):
            column = 0
        self._set_hover_cursor(True)
        self.move_cursor(row=row, column=column, animate=False)
        row_key = self.ordered_rows[row].key
        self._mouse_activation_key = str(row_key.value)
        self._activate_entry(self._entry_for_key(row_key))
        self.call_after_refresh(self._clear_mouse_activation, self._mouse_activation_key)
        event.stop()

    def _clear_mouse_activation(self, key: str) -> None:
        if self._mouse_activation_key == key:
            self._mouse_activation_key = None


__all__ = [
    "Ledger",
    "LedgerOlderClicked",
    "LedgerRecordClicked",
    "LedgerRecordHovered",
    "LedgerRetryClicked",
]
