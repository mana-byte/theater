"""Structured virtualized trajectory event ledger."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from typing import ClassVar

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text
from textual import events
from textual.coordinate import Coordinate
from textual.message import Message
from textual.widgets import DataTable
from textual.widgets.data_table import RowDoesNotExist

from theater.regie.trajectory.constants import (
    LEDGER_CELL_PADDING,
    LEDGER_COMPACT_WIDTH,
    LEDGER_DEFAULT_VIEWPORT_ROWS,
    LEDGER_DETAIL_MIN_HEIGHT,
    LEDGER_DURATION_COLUMN_WIDTH,
    LEDGER_HEADER_HEIGHT,
    LEDGER_MIN_SUMMARY_WIDTH,
    LEDGER_OVERSCAN_ROWS,
    LEDGER_ROW_HEIGHT,
    LEDGER_SCROLLBAR_WIDTH,
    LEDGER_STATUS_COLUMN_WIDTH,
    TRAJECTORY_INSPECTOR_RATIO_DEFAULT,
    TRAJECTORY_INSPECTOR_RATIO_MAX,
    TRAJECTORY_INSPECTOR_RATIO_MIN,
)
from theater.regie.trajectory.details import (
    DETAIL_PARTICIPANT_META,
    DETAIL_TAB_META,
    InlineDetails,
    build_inline_details,
    build_tool_inline_details,
)
from theater.regie.trajectory.enums import InspectorTab, OrderMode
from theater.regie.trajectory.render import (
    format_duration,
    kind_glyph,
    sanitize_text,
    status_label,
    supports_duration_interval,
)
from theater.regie.trajectory.request_rows import request_row_text
from theater.regie.trajectory.search import LedgerEntry, SearchResult
from theater.regie.trajectory.tool_rows import tool_row_text
from theater.trajectory import (
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryRequest,
    TrajectoryStatus,
    TrajectoryToolOperation,
)

RETRY_ACTION_META = "trajectory_retry"


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


class LedgerDetailTabChanged(Message):
    """An inline detail tab was activated."""

    def __init__(self, tab: InspectorTab) -> None:
        super().__init__()
        self.tab = tab


class LedgerParticipantLinkClicked(Message):
    """An inline participant link was activated."""

    def __init__(self, participant_id: str) -> None:
        super().__init__()
        self.participant_id = participant_id


@dataclass(frozen=True, slots=True)
class _DetailRow:
    record_id: str


class Ledger(DataTable[Text | str]):
    """Render records as native table rows with content-sized columns."""

    can_focus = True
    COLUMN_POSITION = "position"
    COLUMN_EVENT = "event"
    COLUMN_SOURCE = "source"
    COLUMN_SUMMARY = "summary"
    COLUMN_STATUS = "status"
    COLUMN_DURATION = "duration"
    EMPTY_KEY = "__empty__"
    OLDER_KEY = "__older__"
    RETRY_KEY = "__retry__"
    GROUP_PREFIX = "group:"
    REQUEST_PREFIX = "request:"
    RECORD_PREFIX = "record:"
    TOOL_PREFIX = "tool:"
    DETAIL_PREFIX = "detail:"
    COLUMN_LABELS: ClassVar[dict[str, str]] = {
        COLUMN_POSITION: "#",
        COLUMN_EVENT: "EVENT",
        COLUMN_SOURCE: "SOURCE",
        COLUMN_SUMMARY: "SUMMARY",
        COLUMN_STATUS: "STATE",
        COLUMN_DURATION: "TIME",
    }

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
        scrollbar-size: 1 1;
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
        background: $accent 4%;
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
        expanded_id: str | None = None,
        detail_tab: InspectorTab = InspectorTab.SUMMARY,
        detail_ratio: float = TRAJECTORY_INSPECTOR_RATIO_DEFAULT,
        position_offset: int = 0,
        **kwargs,
    ) -> None:
        super().__init__(
            show_header=True,
            show_row_labels=False,
            fixed_columns=2,
            zebra_stripes=True,
            cursor_type="row",
            cell_padding=LEDGER_CELL_PADDING,
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
        self._row_entries: dict[str, LedgerEntry | _DetailRow | str] = {}
        self._selected_id = selected_id
        self._hovered_id = hovered_id
        self._order_mode = order_mode
        self._has_older = has_older
        self._loading_older = loading_older
        self._retry_message = retry_message
        self._expanded_id = self._row_id_for_record(expanded_id)
        self._detail_tab = detail_tab
        self._detail_ratio = self._validated_detail_ratio(detail_ratio)
        self._position_offset = max(0, int(position_offset))
        self._detail: InlineDetails | None = None
        self._detail_height_limit = LEDGER_DETAIL_MIN_HEIGHT
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
                expanded_id=expanded_id,
                detail_tab=detail_tab,
                detail_ratio=detail_ratio,
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
    def expanded_id(self) -> str | None:
        return self._expanded_id if self._detail_entry_index is not None else None

    @property
    def detail_tab(self) -> InspectorTab:
        return self._detail_tab

    @property
    def detail_ratio(self) -> float:
        return self._detail_ratio

    @property
    def copy_text(self) -> str:
        return self._detail.copy_text if self._detail is not None else ""

    @property
    def retry_line_index(self) -> int | None:
        if not self._retry_message:
            return None
        return self._row_prefix + len(self._entries) + int(self._detail_entry_index is not None)

    @property
    def _row_prefix(self) -> int:
        return int(self._has_older)

    @property
    def _detail_entry_index(self) -> int | None:
        if self._expanded_id is None:
            return None
        return self._entry_indices.get(self._expanded_id)

    @property
    def _detail_row_index(self) -> int | None:
        index = self._detail_entry_index
        return None if index is None else self._row_prefix + index + 1

    def _viewport_rows(self) -> int:
        if self._viewport_height:
            return self._viewport_height
        height = self.region.height - self.header_height
        return max(1, height // LEDGER_ROW_HEIGHT) if height else LEDGER_DEFAULT_VIEWPORT_ROWS

    def _viewport_content_height(self) -> int:
        if self._viewport_height:
            return self._viewport_height * LEDGER_ROW_HEIGHT
        return max(1, self.region.height - self.header_height)

    @staticmethod
    def _validated_detail_ratio(ratio: float) -> float:
        if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or not isfinite(ratio):
            raise ValueError("detail ratio must be finite")
        return max(
            TRAJECTORY_INSPECTOR_RATIO_MIN,
            min(TRAJECTORY_INSPECTOR_RATIO_MAX, float(ratio)),
        )

    def _detail_limit_for_height(self, height: int) -> int:
        available = max(1, height - self.header_height)
        return max(
            1, min(available, max(LEDGER_DETAIL_MIN_HEIGHT, round(available * self._detail_ratio)))
        )

    def _row_key(self, entry: LedgerEntry) -> str:
        if entry.is_request_header:
            return f"{self.REQUEST_PREFIX}{entry.request_id}"
        if entry.is_group_header:
            return f"{self.GROUP_PREFIX}{entry.group_id}"
        if entry.is_tool_operation:
            return f"{self.TOOL_PREFIX}{entry.tool_operation_id}"
        return f"{self.RECORD_PREFIX}{entry.record_id}"

    def _entry_for_key(self, key: object) -> LedgerEntry | _DetailRow | str | None:
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
        detail_index = self._detail_entry_index
        return self._row_prefix + index + int(detail_index is not None and index > detail_index)

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

    def _lane_style(self, lane: TrajectoryLane) -> Style:
        return {
            TrajectoryLane.INPUT: self._component("input"),
            TrajectoryLane.MODEL: self._component("model"),
            TrajectoryLane.TOOLS: self._component("tools"),
            TrajectoryLane.THEATER: self._component("theater"),
        }[lane]

    def _status_style(self, status: TrajectoryStatus) -> Style:
        if status is TrajectoryStatus.COMPLETED:
            return self._component("muted")
        if status in {TrajectoryStatus.RUNNING, TrajectoryStatus.PENDING, TrajectoryStatus.PARTIAL}:
            return self._component("warning")
        if status in {
            TrajectoryStatus.ERROR,
            TrajectoryStatus.INTERRUPTED,
            TrajectoryStatus.CANCELLED,
            TrajectoryStatus.TIMEOUT,
        }:
            return self._component("error")
        return self._component("muted")

    def _record_values(self, record: TrajectoryRecord, index: int, *, depth: int) -> dict[str, str]:
        marker = (
            "▾"
            if record.record_id == self.expanded_id
            else "●"
            if record.record_id == self._hovered_id
            else "▸"
        )
        summary = f"{'  ' * depth}{sanitize_text(record.summary)}"
        if self._compact_columns:
            summary = f"[{sanitize_text(record.source)}] {summary}"
        return {
            self.COLUMN_POSITION: f"{marker}{index + 1:>3}",
            self.COLUMN_EVENT: (
                f"{kind_glyph(record.kind)} {record.kind.value.replace('_', ' ').upper()}"
            ),
            self.COLUMN_SOURCE: sanitize_text(record.source),
            self.COLUMN_SUMMARY: summary,
            self.COLUMN_STATUS: f"● {status_label(record.status)}",
            self.COLUMN_DURATION: format_duration(record.timing),
        }

    def _record_cells(self, record: TrajectoryRecord, index: int, *, depth: int) -> dict[str, Text]:
        values = self._record_values(record, index, depth=depth)
        hovered = record.record_id == self._hovered_id
        position = Text(values[self.COLUMN_POSITION], style="bold" if hovered else "dim")
        event = Text()
        event.append(kind_glyph(record.kind), style=self._lane_style(record.lane))
        event.append(f" {record.kind.value.replace('_', ' ').upper()}", style="bold")
        source = Text(values[self.COLUMN_SOURCE], style="dim")
        summary = Text(values[self.COLUMN_SUMMARY])
        if hovered:
            summary.stylize("bold")
        status = Text(no_wrap=True)
        status.append("●", style=self._status_style(record.status))
        status.append(f" {status_label(record.status)}", style="dim")
        duration = Text(values[self.COLUMN_DURATION], justify="right")
        if self._order_mode is OrderMode.DURATION and supports_duration_interval(record):
            duration.stylize(self._component("accent") + Style(bold=True))
        return {
            self.COLUMN_POSITION: position,
            self.COLUMN_EVENT: event,
            self.COLUMN_SOURCE: source,
            self.COLUMN_SUMMARY: summary,
            self.COLUMN_STATUS: status,
            self.COLUMN_DURATION: duration,
        }

    def _tool_cells(self, entry: LedgerEntry, index: int) -> dict[str, Text]:
        tool = self._tools.get(entry.tool_operation_id or "")
        if tool is None:
            return self._record_cells(
                self._records[entry.record_id or ""], index, depth=entry.depth
            )
        text = tool_row_text(tool, compact=self._compact_columns)
        hovered = entry.record_id == self._row_id_for_record(self._hovered_id)
        marker = (
            "▾"
            if entry.record_id == self._row_id_for_record(self._expanded_id)
            else "●"
            if hovered
            else "▸"
        )
        status = Text(no_wrap=True)
        status.append("●", style=self._status_style(tool.status))
        status.append(f" {text.status}", style="dim")
        return {
            self.COLUMN_POSITION: Text(
                f"{marker}{index + 1:>3}", style="bold" if hovered else "dim"
            ),
            self.COLUMN_EVENT: Text(text.event, style=self._component("tools") + Style(bold=True)),
            self.COLUMN_SOURCE: Text(text.source, style="dim"),
            self.COLUMN_SUMMARY: Text(
                f"{'  ' * entry.depth}{text.summary}", style="bold" if hovered else ""
            ),
            self.COLUMN_STATUS: status,
            self.COLUMN_DURATION: Text(text.duration, justify="right"),
        }

    def _tool_values(self, entry: LedgerEntry, index: int) -> dict[str, str]:
        return {key: value.plain for key, value in self._tool_cells(entry, index).items()}

    def _group_values(self, entry: LedgerEntry) -> dict[str, str]:
        kind = entry.group_kind.value.replace("_", " ").upper() if entry.group_kind else "GROUP"
        label = sanitize_text(entry.group_label)
        return {
            self.COLUMN_POSITION: "",
            self.COLUMN_EVENT: kind,
            self.COLUMN_SOURCE: "",
            self.COLUMN_SUMMARY: f"{'  ' * entry.depth}{label}",
            self.COLUMN_STATUS: "",
            self.COLUMN_DURATION: "",
        }

    def _group_cells(self, entry: LedgerEntry) -> dict[str, Text | str]:
        values = self._group_values(entry)
        return {
            self.COLUMN_POSITION: "",
            self.COLUMN_EVENT: Text(values[self.COLUMN_EVENT], style="bold"),
            self.COLUMN_SOURCE: "",
            self.COLUMN_SUMMARY: Text(values[self.COLUMN_SUMMARY], style="bold"),
            self.COLUMN_STATUS: Text(values[self.COLUMN_STATUS], style="dim"),
            self.COLUMN_DURATION: "",
        }

    def _request_values(self, entry: LedgerEntry) -> dict[str, str]:
        request = self._requests.get(entry.request_id or "")
        if request is None:
            return {
                self.COLUMN_POSITION: "╭",
                self.COLUMN_EVENT: "◆ REQUEST",
                self.COLUMN_SOURCE: "model unknown",
                self.COLUMN_SUMMARY: "usage unavailable",
                self.COLUMN_STATUS: "● unknown",
                self.COLUMN_DURATION: "—",
            }
        text = request_row_text(request, compact=self._compact_columns)
        return {
            self.COLUMN_POSITION: "╭",
            self.COLUMN_EVENT: text.event,
            self.COLUMN_SOURCE: text.source,
            self.COLUMN_SUMMARY: f"{'  ' * entry.depth}{text.summary}",
            self.COLUMN_STATUS: f"● {text.status}",
            self.COLUMN_DURATION: text.duration,
        }

    def _request_cells(self, entry: LedgerEntry) -> dict[str, Text | str]:
        values = self._request_values(entry)
        request = self._requests.get(entry.request_id or "")
        status = request.status if request is not None else TrajectoryStatus.UNKNOWN
        request_style = self._component("request")
        dim_request_style = request_style + Style(dim=True)
        event = Text(values[self.COLUMN_EVENT], style=request_style + Style(bold=True))
        state = Text(no_wrap=True)
        state.append("●", style=request_style + self._status_style(status))
        state.append(f" {status_label(status)}", style=dim_request_style)
        return {
            self.COLUMN_POSITION: Text(values[self.COLUMN_POSITION], style=request_style),
            self.COLUMN_EVENT: event,
            self.COLUMN_SOURCE: Text(values[self.COLUMN_SOURCE], style=dim_request_style),
            self.COLUMN_SUMMARY: Text(values[self.COLUMN_SUMMARY], style=dim_request_style),
            self.COLUMN_STATUS: state,
            self.COLUMN_DURATION: Text(
                values[self.COLUMN_DURATION], justify="right", style=dim_request_style
            ),
        }

    def _older_values(self) -> dict[str, str]:
        loading = self._loading_older
        return {
            self.COLUMN_POSITION: "…" if loading else "↑",
            self.COLUMN_EVENT: "HISTORY",
            self.COLUMN_SUMMARY: "Loading earlier events…" if loading else "Load earlier events",
            self.COLUMN_STATUS: "waiting" if loading else "activate",
        }

    def _empty_values(self) -> dict[str, str]:
        return {
            self.COLUMN_EVENT: "EMPTY",
            self.COLUMN_SUMMARY: "No loaded records match the current search or filters.",
        }

    def _retry_values(self) -> dict[str, str]:
        retry = sanitize_text(self._retry_message or "").replace("\r", " ").replace("\n", " ")
        return {
            self.COLUMN_POSITION: "!",
            self.COLUMN_EVENT: "ERROR",
            self.COLUMN_SUMMARY: retry,
            self.COLUMN_STATUS: " ↻ Retry ",
        }

    def _build_detail(self) -> InlineDetails | None:
        expanded_row_id = self._row_id_for_record(self._expanded_id)
        for entry in self._entries:
            if entry.record_id == expanded_row_id and entry.is_tool_operation:
                tool = self._tools.get(entry.tool_operation_id or "")
                if tool is not None:
                    detail = build_tool_inline_details(
                        tool,
                        self._detail_tab,
                        max_height=self._detail_height_limit,
                        accent_style=self._component("accent"),
                    )
                    self._detail_tab = detail.tab
                    return detail
        record = self._records.get(self._expanded_id or "")
        if record is None or self._detail_entry_index is None:
            return None
        detail = build_inline_details(
            record,
            self._detail_tab,
            max_height=self._detail_height_limit,
            accent_style=self._component("accent"),
        )
        self._detail_tab = detail.tab
        return detail

    def _detail_values(self, detail: InlineDetails) -> dict[str, str]:
        return {
            self.COLUMN_POSITION: "╰",
            self.COLUMN_EVENT: detail.menu.plain,
            self.COLUMN_SOURCE: "",
            self.COLUMN_SUMMARY: detail.content.plain,
            self.COLUMN_STATUS: "",
            self.COLUMN_DURATION: "",
        }

    def _measure_column_widths(self) -> dict[str, int]:
        widths = {key: self._text_width(label) for key, label in self.COLUMN_LABELS.items()}

        def include(values: Mapping[str, str]) -> None:
            for key, value in values.items():
                widths[key] = max(widths[key], self._text_width(value))

        if self._has_older:
            include(self._older_values())
        if not self._entries and not self._retry_message and not self._has_older:
            include(self._empty_values())
        for line_index, entry in enumerate(self._entries):
            if entry.is_request_header:
                include(self._request_values(entry))
                continue
            if entry.is_group_header:
                include(self._group_values(entry))
                continue
            record = self._records.get(entry.record_id or "")
            if entry.is_tool_operation:
                include(self._tool_values(entry, self._record_indices[line_index] or 0))
            elif record is not None:
                include(
                    self._record_values(
                        record,
                        self._record_indices[line_index] or 0,
                        depth=entry.depth,
                    )
                )
        if self._detail is not None:
            include(self._detail_values(self._detail))
        if self._retry_message:
            include(self._retry_values())
        if any(entry.is_tool_operation for entry in self._entries):
            widths[self.COLUMN_STATUS] = LEDGER_STATUS_COLUMN_WIDTH
            widths[self.COLUMN_DURATION] = LEDGER_DURATION_COLUMN_WIDTH
        return widths

    @staticmethod
    def _middle_cell(value: Text | str) -> Text:
        centered = Text("\n")
        if isinstance(value, Text):
            centered.append_text(value)
            centered.justify = value.justify
        else:
            centered.append(value)
        centered.no_wrap = True
        centered.overflow = "ellipsis"
        return centered

    def _add_cells(self, cells: Mapping[str, Text | str], *, key: str) -> None:
        self.add_row(
            *(self._middle_cell(cells.get(column, "")) for column in self._column_keys),
            height=LEDGER_ROW_HEIGHT,
            key=key,
        )

    def _add_detail_row(self, record_id: str, detail: InlineDetails) -> None:
        key = f"{self.DETAIL_PREFIX}{record_id}"
        cells: dict[str, Text | str] = {
            self.COLUMN_POSITION: Text("╰", style=self._component("accent")),
            self.COLUMN_EVENT: detail.menu,
            self.COLUMN_SOURCE: "",
            self.COLUMN_SUMMARY: detail.content,
            self.COLUMN_STATUS: "",
            self.COLUMN_DURATION: "",
        }
        self.add_row(
            *(cells.get(column, "") for column in self._column_keys),
            height=detail.height,
            key=key,
        )
        self._row_entries[key] = _DetailRow(record_id)

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
            (
                self.expanded_id,
                self._detail_tab,
                self._detail_height_limit,
            ),
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

    def _populate_rows(self) -> None:
        self._ensure_columns()
        self._row_entries.clear()
        if self._has_older:
            loading = self._loading_older
            values = self._older_values()
            self._add_cells(
                {
                    self.COLUMN_POSITION: Text(
                        values[self.COLUMN_POSITION],
                        style=self._component("accent") + Style(bold=True),
                    ),
                    self.COLUMN_EVENT: Text(
                        values[self.COLUMN_EVENT],
                        style=self._component("accent") + Style(bold=True),
                    ),
                    self.COLUMN_SUMMARY: Text(
                        values[self.COLUMN_SUMMARY],
                        style="dim" if loading else self._component("accent"),
                    ),
                    self.COLUMN_STATUS: Text(values[self.COLUMN_STATUS], style="dim"),
                },
                key=self.OLDER_KEY,
            )
            self._row_entries[self.OLDER_KEY] = self.OLDER_KEY
        if not self._entries and not self._retry_message and not self._has_older:
            values = self._empty_values()
            self._add_cells(
                {
                    self.COLUMN_EVENT: Text(values[self.COLUMN_EVENT], style="dim"),
                    self.COLUMN_SUMMARY: Text(values[self.COLUMN_SUMMARY], style="dim"),
                },
                key=self.EMPTY_KEY,
            )
            self._row_entries[self.EMPTY_KEY] = self.EMPTY_KEY
            return
        for line_index, entry in enumerate(self._entries):
            key = self._row_key(entry)
            self._row_entries[key] = entry
            if entry.is_request_header:
                self._add_cells(self._request_cells(entry), key=key)
                continue
            if entry.is_group_header:
                self._add_cells(self._group_cells(entry), key=key)
                continue
            record = self._records.get(entry.record_id or "")
            if record is None:
                continue
            record_index = self._record_indices[line_index] or 0
            cells = (
                self._tool_cells(entry, record_index)
                if entry.is_tool_operation
                else self._record_cells(record, record_index, depth=entry.depth)
            )
            self._add_cells(cells, key=key)
            if entry.record_id == self.expanded_id and self._detail is not None:
                self._add_detail_row(record.record_id, self._detail)
        if self._retry_message:
            values = self._retry_values()
            self._add_cells(
                {
                    self.COLUMN_POSITION: Text(
                        values[self.COLUMN_POSITION],
                        style=self._component("warning") + Style(bold=True),
                    ),
                    self.COLUMN_EVENT: Text(
                        values[self.COLUMN_EVENT],
                        style=self._component("warning") + Style(bold=True),
                    ),
                    self.COLUMN_SUMMARY: Text(
                        values[self.COLUMN_SUMMARY],
                        style=self._component("warning"),
                    ),
                    self.COLUMN_STATUS: Text(
                        values[self.COLUMN_STATUS],
                        style=self._component("retry") + Style(meta={RETRY_ACTION_META: True}),
                    ),
                },
                key=self.RETRY_KEY,
            )
            self._row_entries[self.RETRY_KEY] = self.RETRY_KEY

    def _rebuild(self, *, preserve_scroll: bool = True) -> None:
        previous_scroll = self._scroll_offset if preserve_scroll else 0
        self._building = True
        try:
            self.clear(columns=True)
            self._ensure_columns()
            self._populate_rows()
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
        self._sync_selection()

    def _update_row_starts(self) -> None:
        starts: list[int] = []
        offset = 0
        for row in self.ordered_rows:
            starts.append(offset)
            offset += row.height
        self._row_starts = tuple(starts)
        self._rows_height = offset

    def _update_detail_row(self) -> None:
        detail = self._build_detail()
        self._detail = detail
        record_id = self.expanded_id
        if detail is None or record_id is None:
            return
        key = f"{self.DETAIL_PREFIX}{record_id}"
        try:
            row_index = self.get_row_index(key)
        except RowDoesNotExist:
            self._rebuild()
            return
        cells: dict[str, Text | str] = {
            self.COLUMN_POSITION: Text("╰", style=self._component("accent")),
            self.COLUMN_EVENT: detail.menu,
            self.COLUMN_SOURCE: "",
            self.COLUMN_SUMMARY: detail.content,
            self.COLUMN_STATUS: "",
            self.COLUMN_DURATION: "",
        }
        for column in self._column_keys:
            self.update_cell(key, column, cells.get(column, ""))
        row = self.ordered_rows[row_index]
        if row.height != detail.height:
            row.height = detail.height
            self._require_update_dimensions = True
            self.check_idle()
            self.refresh(layout=True)
        self._update_row_starts()
        self._structure = self._structure_key()

    def _refresh_changed_records(self) -> None:
        for line_index, entry in enumerate(self._entries):
            if entry.is_header or entry.is_tool_operation or entry.record_id is None:
                continue
            record = self._records.get(entry.record_id)
            if record is None or self._revisions.get(entry.record_id) == record.revision:
                continue
            key = self._row_key(entry)
            cells = self._record_cells(
                record, self._record_indices[line_index] or 0, depth=entry.depth
            )
            for column in self._column_keys:
                self.update_cell(key, column, self._middle_cell(cells[column]))
            self._revisions[entry.record_id] = record.revision

    def _refresh_changed_requests(self) -> None:
        for entry in self._entries:
            if not entry.is_request_header or entry.request_id is None:
                continue
            request = self._requests.get(entry.request_id)
            if request is None or self._rendered_requests.get(entry.request_id) == request:
                continue
            key = self._row_key(entry)
            cells = self._request_cells(entry)
            for column in self._column_keys:
                self.update_cell(key, column, self._middle_cell(cells[column]))
            self._rendered_requests[entry.request_id] = request

    def _refresh_changed_tools(self) -> None:
        for line_index, entry in enumerate(self._entries):
            if not entry.is_tool_operation or entry.tool_operation_id is None:
                continue
            tool = self._tools.get(entry.tool_operation_id)
            if tool is None or self._rendered_tools.get(entry.tool_operation_id) == tool:
                continue
            cells = self._tool_cells(entry, self._record_indices[line_index] or 0)
            for column in self._column_keys:
                self.update_cell(self._row_key(entry), column, self._middle_cell(cells[column]))
            self._rendered_tools[entry.tool_operation_id] = tool

    def update_rows(  # noqa: PLR0915
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
        expanded_id: str | None = None,
        detail_tab: InspectorTab = InspectorTab.SUMMARY,
        detail_ratio: float = TRAJECTORY_INSPECTOR_RATIO_DEFAULT,
        position_offset: int = 0,
    ) -> None:
        old_selected_line = self._row_index_for_record(self._selected_id)
        old_selected = self._selected_id
        previous_expanded_id = self.expanded_id
        previous_detail_tab = self._detail_tab
        previous_detail_limit = self._detail_height_limit
        previous_detail_revision = self._revisions.get(self.expanded_id or "")
        previous_detail_tool = next(
            (
                self._tools[entry.tool_operation_id]
                for entry in self._entries
                if entry.record_id == self._row_id_for_record(self.expanded_id)
                and entry.tool_operation_id in self._tools
            ),
            None,
        )
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
        self._selected_id = selected_id
        self._hovered_id = hovered_id
        self._order_mode = order_mode
        self._has_older = has_older
        self._loading_older = loading_older
        self._retry_message = retry_message
        self._expanded_id = self._row_id_for_record(expanded_id)
        self._detail_tab = detail_tab
        self._detail_ratio = self._validated_detail_ratio(detail_ratio)
        self._position_offset = max(0, int(position_offset))
        if self.region.height:
            self._detail_height_limit = self._detail_limit_for_height(self.region.height)
        self._line_ids = tuple(entry.record_id for entry in self._entries)
        self._record_indices = self._record_index_map()
        if (
            self._expanded_id != previous_expanded_id
            or self._detail_tab != previous_detail_tab
            or self._detail_height_limit != previous_detail_limit
        ):
            self._detail = self._build_detail()
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
            detail_changed = (
                self.expanded_id is not None
                and previous_detail_revision != self._records[self.expanded_id].revision
            )
            self._refresh_changed_records()
            self._refresh_changed_requests()
            self._refresh_changed_tools()
            current_detail_tool = next(
                (
                    self._tools[entry.tool_operation_id]
                    for entry in self._entries
                    if entry.record_id == self._row_id_for_record(self.expanded_id)
                    and entry.tool_operation_id in self._tools
                ),
                None,
            )
            if current_detail_tool != previous_detail_tool:
                self._update_detail_row()
            if detail_changed:
                self._update_detail_row()
            self._sync_selection()
            self.set_scroll_offset(self._scroll_offset)
        self._update_render_window()

    def set_details(
        self,
        expanded_id: str | None,
        tab: InspectorTab,
        *,
        detail_ratio: float | None = None,
    ) -> None:
        if detail_ratio is not None:
            self._detail_ratio = self._validated_detail_ratio(detail_ratio)
            if self.region.height:
                self._detail_height_limit = self._detail_limit_for_height(self.region.height)
        expanded_id = self._row_id_for_record(expanded_id)
        same_record = expanded_id == self.expanded_id
        self._expanded_id = expanded_id
        self._detail_tab = tab
        if same_record and expanded_id is not None and self._detail is not None:
            self._update_detail_row()
        elif same_record and expanded_id is None:
            return
        else:
            self._detail = self._build_detail()
            self._column_widths = self._measure_column_widths()
            self._summary_width = self._columns_width()
            self._rebuild()
        if expanded_id is not None:
            self.scroll_to_record(expanded_id, include_detail=True)

    def _sync_selection(self) -> None:
        line = self._row_index_for_record(self._selected_id)
        if line is None or not self.row_count:
            return
        self._syncing_cursor = True
        try:
            self.move_cursor(row=line, column=0, animate=False, scroll=False)
        finally:
            self._syncing_cursor = False

    def _total_lines(self) -> int:
        return max(
            1,
            self._row_prefix
            + len(self._entries)
            + int(self._detail_entry_index is not None)
            + (1 if self._retry_message else 0),
        )

    def _max_scroll_row(self) -> int:
        if not self._row_starts:
            return max(0, self._total_lines() - self._viewport_rows())
        viewport_height = self._viewport_content_height()
        max_y = max(0, self._rows_height - viewport_height)
        return max(0, bisect_right(self._row_starts, max_y) - 1)

    def _clamp_scroll(self) -> None:
        self._scroll_offset = max(0, min(self._scroll_offset, self._max_scroll_row()))

    def _update_render_window(self) -> None:
        self._clamp_scroll()
        start = max(0, self._scroll_offset - LEDGER_OVERSCAN_ROWS)
        end = min(
            len(self.ordered_rows),
            self._scroll_offset + self._viewport_rows() + LEDGER_OVERSCAN_ROWS,
        )
        visible = [
            self._row_entries.get(str(row.key.value)) for row in self.ordered_rows[start:end]
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

    def scroll_to_record(self, record_id: str | None, *, include_detail: bool = False) -> int:
        record_id = self._row_id_for_record(record_id)
        line = self._row_index_for_record(record_id)
        if line is None:
            return self._scroll_offset
        last_line = (
            self._detail_row_index if include_detail and record_id == self.expanded_id else line
        )
        if last_line is None or line >= len(self._row_starts) or last_line >= len(self._row_starts):
            return self._scroll_offset
        top = self._row_starts[line]
        bottom = self._row_starts[last_line] + self.ordered_rows[last_line].height
        viewport_height = self._viewport_content_height()
        current_y = int(self.scroll_y)
        target_y = current_y
        if top < current_y:
            target_y = top
        elif bottom > current_y + viewport_height:
            target_y = max(top, bottom - viewport_height)
        if target_y != current_y:
            max_y = max(0, self._rows_height - viewport_height)
            target_y = min(target_y, max_y)
            self._scroll_offset = max(0, bisect_right(self._row_starts, target_y) - 1)
            self.scroll_to(y=target_y, animate=False, force=True)
            self._update_render_window()
        return self._scroll_offset

    def set_hovered(self, record_id: str | None) -> None:
        record_id = record_id if record_id in self._records else None
        if record_id == self._hovered_id:
            return
        previous = self._hovered_id
        self._hovered_id = record_id
        for candidate in (previous, record_id):
            line_index = self._entry_index_for_record(candidate)
            if line_index is None or candidate not in self._records:
                continue
            entry = self._entries[line_index]
            cells = (
                self._tool_cells(entry, self._record_indices[line_index] or 0)
                if entry.is_tool_operation
                else self._record_cells(
                    self._records[candidate],
                    self._record_indices[line_index] or 0,
                    depth=entry.depth,
                )
            )
            self.update_cell(
                self._row_key(entry),
                self.COLUMN_POSITION,
                self._middle_cell(cells[self.COLUMN_POSITION]),
            )
            self.update_cell(
                self._row_key(entry),
                self.COLUMN_SUMMARY,
                self._middle_cell(cells[self.COLUMN_SUMMARY]),
            )

    def set_selected(self, record_id: str | None) -> None:
        index = self._entry_index_for_record(record_id)
        if index is not None:
            record_id = self._entries[index].record_id
        if record_id == self._selected_id:
            return
        self._selected_id = record_id
        self._sync_selection()

    def on_resize(self, event: events.Resize) -> None:
        self._viewport_height = max(
            1,
            (event.size.height - self.header_height) // LEDGER_ROW_HEIGHT,
        )
        compact_columns = event.size.width < LEDGER_COMPACT_WIDTH
        columns_changed = compact_columns != self._compact_columns
        detail_height = self._detail_limit_for_height(event.size.height)
        detail_height_changed = detail_height != self._detail_height_limit
        self._detail_height_limit = detail_height
        if columns_changed:
            self._compact_columns = compact_columns
        if columns_changed or detail_height_changed:
            self._column_widths = self._measure_column_widths()
        summary_width = self._columns_width(event.size.width)
        if summary_width != self._summary_width or columns_changed or detail_height_changed:
            self._summary_width = summary_width
            if len(self.columns):
                self._rebuild()
        self._update_render_window()

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        self._scroll_offset = max(0, bisect_right(self._row_starts, int(new_value)) - 1)
        self._update_render_window()

    def watch_hover_coordinate(self, old: Coordinate, value: Coordinate) -> None:
        super().watch_hover_coordinate(old, value)
        if self._building or not self._show_hover_cursor or old.row == value.row:
            return
        if not self.is_valid_coordinate(value):
            self.post_message(LedgerRecordHovered(None))
            return
        key = self.coordinate_to_cell_key(value).row_key
        entry = self._entry_for_key(key)
        record_id: str | None
        if isinstance(entry, _DetailRow):
            record_id = entry.record_id
        else:
            record_id = (
                entry.record_id if isinstance(entry, LedgerEntry) and not entry.is_header else None
            )
        self.post_message(LedgerRecordHovered(record_id))

    def _on_leave(self, event: events.Leave) -> None:
        super()._on_leave(event)
        self.post_message(LedgerRecordHovered(None))

    def on_data_table_row_selected(self, message: DataTable.RowSelected) -> None:
        if message.data_table is not self:
            return
        entry = self._entry_for_key(message.row_key)
        if entry == self.OLDER_KEY:
            if not self._loading_older:
                self.post_message(LedgerOlderClicked())
        elif entry == self.RETRY_KEY:
            self.post_message(LedgerRetryClicked())
        elif isinstance(entry, _DetailRow) or (isinstance(entry, LedgerEntry) and entry.is_header):
            self._sync_selection()
        elif isinstance(entry, LedgerEntry):
            self._selected_id = entry.record_id
            self.post_message(LedgerRecordClicked(entry.record_id))
        message.stop()

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1:
            return
        meta = event.style.meta
        if meta.get(RETRY_ACTION_META):
            event.stop()
            self.post_message(LedgerRetryClicked())
            return
        if participant_id := meta.get(DETAIL_PARTICIPANT_META):
            if isinstance(participant_id, str):
                event.stop()
                self.post_message(LedgerParticipantLinkClicked(participant_id))
            return
        if tab_value := meta.get(DETAIL_TAB_META):
            try:
                tab = InspectorTab(str(tab_value))
            except ValueError:
                return
            event.stop()
            self.post_message(LedgerDetailTabChanged(tab))
            return
        row = meta.get("row")
        if not isinstance(row, int) or not self.is_valid_row_index(row):
            return
        if isinstance(self._entry_for_key(self.ordered_rows[row].key), _DetailRow):
            owner = max(0, row - 1)
            self.move_cursor(row=owner, column=0, animate=False)
            event.stop()
            return
        self.move_cursor(row=row, column=0, animate=False)


__all__ = [
    "Ledger",
    "LedgerDetailTabChanged",
    "LedgerOlderClicked",
    "LedgerParticipantLinkClicked",
    "LedgerRecordClicked",
    "LedgerRecordHovered",
    "LedgerRetryClicked",
]
