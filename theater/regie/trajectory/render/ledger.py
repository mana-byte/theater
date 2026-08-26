"""Pure row values for the trajectory ledger."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from rich.style import Style
from rich.text import Text

from theater.constants.regie_trajectory import TRAJECTORY_REQUEST_POSITION_GLYPH
from theater.regie.trajectory.render.records import (
    format_duration,
    kind_glyph,
    sanitize_text,
    status_label,
    supports_duration_interval,
)
from theater.regie.trajectory.render.requests import request_row_text
from theater.regie.trajectory.render.tools import tool_row_text
from theater.regie.trajectory.search import LedgerEntry
from theater.trajectory import (
    GroupKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryRequest,
    TrajectoryStatus,
    TrajectoryToolOperation,
)

COLUMN_POSITION = "position"
COLUMN_EVENT = "event"
COLUMN_SOURCE = "source"
COLUMN_SUMMARY = "summary"
COLUMN_STATUS = "status"
COLUMN_DURATION = "duration"
COLUMN_KEYS = (
    COLUMN_POSITION,
    COLUMN_EVENT,
    COLUMN_SOURCE,
    COLUMN_SUMMARY,
    COLUMN_STATUS,
    COLUMN_DURATION,
)
COLUMN_LABELS: Mapping[str, str] = MappingProxyType(
    {
        COLUMN_POSITION: "#",
        COLUMN_EVENT: "EVENT",
        COLUMN_SOURCE: "SOURCE",
        COLUMN_SUMMARY: "SUMMARY",
        COLUMN_STATUS: "STATE",
        COLUMN_DURATION: "TIME",
    }
)


@dataclass(frozen=True, slots=True)
class LedgerRowValues(Mapping[str, str]):
    """Immutable plain values for one ledger row."""

    position: str = ""
    event: str = ""
    source: str = ""
    summary: str = ""
    status: str = ""
    duration: str = ""

    def __getitem__(self, key: str) -> str:
        match key:
            case "position":
                return self.position
            case "event":
                return self.event
            case "source":
                return self.source
            case "summary":
                return self.summary
            case "status":
                return self.status
            case "duration":
                return self.duration
            case _:
                raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(COLUMN_KEYS)

    def __len__(self) -> int:
        return len(COLUMN_KEYS)


@dataclass(frozen=True, slots=True)
class LedgerStylePalette:
    """Resolved Rich styles used by the ledger cell builders."""

    input: Style
    model: Style
    tools: Style
    theater: Style
    muted: Style
    warning: Style
    error: Style
    accent: Style
    retry: Style
    request: Style


def record_values(
    record: TrajectoryRecord,
    index: int,
    *,
    depth: int,
    hovered: bool,
    compact: bool,
) -> LedgerRowValues:
    """Project one record row's plain cells."""
    summary = f"{'  ' * depth}{sanitize_text(record.summary)}"
    if compact:
        summary = f"[{sanitize_text(record.source)}] {summary}"
    return LedgerRowValues(
        position=f"{'●' if hovered else '▸'}{index + 1:>3}",
        event=f"{kind_glyph(record.kind)} {record.kind.value.replace('_', ' ').upper()}",
        source=sanitize_text(record.source),
        summary=summary,
        status=f"● {status_label(record.status)}",
        duration=format_duration(record.timing),
    )


def tool_values(
    tool: TrajectoryToolOperation,
    index: int,
    *,
    depth: int,
    hovered: bool,
    compact: bool,
) -> LedgerRowValues:
    """Project one logical tool-operation row's plain cells."""
    text = tool_row_text(tool, compact=compact)
    return LedgerRowValues(
        position=f"{'●' if hovered else '▸'}{index + 1:>3}",
        event=text.event,
        source=text.source,
        summary=f"{'  ' * depth}{text.summary}",
        status=f"● {text.status}",
        duration=text.duration,
    )


def request_values(
    request: TrajectoryRequest | None,
    *,
    depth: int,
    compact: bool,
) -> LedgerRowValues:
    """Project one request-header row's plain cells."""
    if request is None:
        return LedgerRowValues(
            position=TRAJECTORY_REQUEST_POSITION_GLYPH,
            event="◆ REQUEST",
            source="model unknown",
            summary="usage unavailable",
            status="● unknown",
            duration="—",
        )
    text = request_row_text(request, compact=compact)
    return LedgerRowValues(
        position=TRAJECTORY_REQUEST_POSITION_GLYPH,
        event=text.event,
        source=text.source,
        summary=f"{'  ' * depth}{text.summary}",
        status=f"● {text.status}",
        duration=text.duration,
    )


def group_values(
    group_kind: GroupKind | None,
    group_label: str,
    *,
    depth: int,
) -> LedgerRowValues:
    """Project one nested group-header row's plain cells."""
    kind = group_kind.value.replace("_", " ").upper() if group_kind else "GROUP"
    return LedgerRowValues(event=kind, summary=f"{'  ' * depth}{sanitize_text(group_label)}")


def history_values(*, loading: bool) -> LedgerRowValues:
    """Project the earlier-history activation row."""
    return LedgerRowValues(
        position="…" if loading else "↑",
        event="HISTORY",
        summary="Loading earlier events…" if loading else "Load earlier events",
        status="waiting" if loading else "activate",
    )


def empty_values() -> LedgerRowValues:
    """Project the empty-ledger row."""
    return LedgerRowValues(
        event="EMPTY",
        summary="No loaded records match the current search or filters.",
    )


def retry_values(message: str | None) -> LedgerRowValues:
    """Project the retry activation row."""
    retry = sanitize_text(message or "").replace("\r", " ").replace("\n", " ")
    return LedgerRowValues(position="!", event="ERROR", summary=retry, status=" ↻ Retry ")


def entry_values(
    entry: LedgerEntry,
    *,
    record: TrajectoryRecord | None,
    request: TrajectoryRequest | None,
    tool: TrajectoryToolOperation | None,
    index: int,
    record_hovered: bool,
    entry_hovered: bool,
    compact: bool,
) -> LedgerRowValues | None:
    """Project any visible search entry into its canonical row values."""
    if entry.is_request_header:
        return request_values(request, depth=entry.depth, compact=compact)
    if entry.is_group_header:
        return group_values(entry.group_kind, entry.group_label, depth=entry.depth)
    if record is None:
        return None
    if entry.is_tool_operation and tool is not None:
        return tool_values(tool, index, depth=entry.depth, hovered=entry_hovered, compact=compact)
    return record_values(record, index, depth=entry.depth, hovered=record_hovered, compact=compact)


def lane_style(lane: TrajectoryLane, palette: LedgerStylePalette) -> Style:
    """Return the resolved lane style for one record."""
    return {
        TrajectoryLane.INPUT: palette.input,
        TrajectoryLane.MODEL: palette.model,
        TrajectoryLane.TOOLS: palette.tools,
        TrajectoryLane.THEATER: palette.theater,
    }[lane]


def status_style(status: TrajectoryStatus, palette: LedgerStylePalette) -> Style:
    """Return the resolved status style for one terminal state."""
    if status is TrajectoryStatus.COMPLETED:
        return palette.muted
    if status in {TrajectoryStatus.RUNNING, TrajectoryStatus.PENDING, TrajectoryStatus.PARTIAL}:
        return palette.warning
    if status in {
        TrajectoryStatus.ERROR,
        TrajectoryStatus.INTERRUPTED,
        TrajectoryStatus.CANCELLED,
        TrajectoryStatus.TIMEOUT,
    }:
        return palette.error
    return palette.muted


def record_cells(
    record: TrajectoryRecord,
    values: LedgerRowValues,
    palette: LedgerStylePalette,
    *,
    hovered: bool,
    duration_mode: bool,
) -> dict[str, Text]:
    """Build Rich cells for one canonical record projection."""
    position = Text(values[COLUMN_POSITION], style="bold" if hovered else "dim")
    event = Text()
    glyph = kind_glyph(record.kind)
    event.append(glyph, style=lane_style(record.lane, palette))
    event.append(values[COLUMN_EVENT][len(glyph) :], style="bold")
    summary = Text(values[COLUMN_SUMMARY])
    if hovered:
        summary.stylize("bold")
    status = Text(no_wrap=True)
    status.append("●", style=status_style(record.status, palette))
    status.append(values[COLUMN_STATUS][1:], style="dim")
    duration = Text(values[COLUMN_DURATION], justify="right")
    if duration_mode and supports_duration_interval(record):
        duration.stylize(palette.accent + Style(bold=True))
    return {
        COLUMN_POSITION: position,
        COLUMN_EVENT: event,
        COLUMN_SOURCE: Text(values[COLUMN_SOURCE], style="dim"),
        COLUMN_SUMMARY: summary,
        COLUMN_STATUS: status,
        COLUMN_DURATION: duration,
    }


def tool_cells(
    tool: TrajectoryToolOperation,
    values: LedgerRowValues,
    palette: LedgerStylePalette,
    *,
    hovered: bool,
) -> dict[str, Text]:
    """Build Rich cells for one canonical tool projection."""
    status = Text(no_wrap=True)
    status.append("●", style=status_style(tool.status, palette))
    status.append(values[COLUMN_STATUS][1:], style="dim")
    return {
        COLUMN_POSITION: Text(values[COLUMN_POSITION], style="bold" if hovered else "dim"),
        COLUMN_EVENT: Text(values[COLUMN_EVENT], style=palette.tools + Style(bold=True)),
        COLUMN_SOURCE: Text(values[COLUMN_SOURCE], style="dim"),
        COLUMN_SUMMARY: Text(values[COLUMN_SUMMARY], style="bold" if hovered else ""),
        COLUMN_STATUS: status,
        COLUMN_DURATION: Text(values[COLUMN_DURATION], justify="right"),
    }


def request_cells(
    request: TrajectoryRequest | None,
    values: LedgerRowValues,
    palette: LedgerStylePalette,
) -> dict[str, Text]:
    """Build Rich cells for one canonical request projection."""
    status = request.status if request is not None else TrajectoryStatus.UNKNOWN
    dim_request_style = palette.request + Style(dim=True)
    state = Text(no_wrap=True)
    state.append("●", style=palette.request + status_style(status, palette))
    state.append(values[COLUMN_STATUS][1:], style=dim_request_style)
    return {
        COLUMN_POSITION: Text(values[COLUMN_POSITION], style=palette.request),
        COLUMN_EVENT: Text(values[COLUMN_EVENT], style=palette.request + Style(bold=True)),
        COLUMN_SOURCE: Text(values[COLUMN_SOURCE], style=dim_request_style),
        COLUMN_SUMMARY: Text(values[COLUMN_SUMMARY], style=dim_request_style),
        COLUMN_STATUS: state,
        COLUMN_DURATION: Text(values[COLUMN_DURATION], justify="right", style=dim_request_style),
    }


def group_cells(values: LedgerRowValues) -> dict[str, Text | str]:
    """Build Rich cells for one canonical group projection."""
    return {
        COLUMN_POSITION: "",
        COLUMN_EVENT: Text(values[COLUMN_EVENT], style="bold"),
        COLUMN_SOURCE: "",
        COLUMN_SUMMARY: Text(values[COLUMN_SUMMARY], style="bold"),
        COLUMN_STATUS: Text(values[COLUMN_STATUS], style="dim"),
        COLUMN_DURATION: "",
    }


def history_cells(
    values: LedgerRowValues,
    palette: LedgerStylePalette,
    *,
    loading: bool,
) -> dict[str, Text]:
    """Build Rich cells for the earlier-history row."""
    return {
        COLUMN_POSITION: Text(values[COLUMN_POSITION], style=palette.accent + Style(bold=True)),
        COLUMN_EVENT: Text(values[COLUMN_EVENT], style=palette.accent + Style(bold=True)),
        COLUMN_SUMMARY: Text(values[COLUMN_SUMMARY], style="dim" if loading else palette.accent),
        COLUMN_STATUS: Text(values[COLUMN_STATUS], style="dim"),
    }


def empty_cells(values: LedgerRowValues) -> dict[str, Text]:
    """Build Rich cells for the empty-ledger row."""
    return {
        COLUMN_EVENT: Text(values[COLUMN_EVENT], style="dim"),
        COLUMN_SUMMARY: Text(values[COLUMN_SUMMARY], style="dim"),
    }


def retry_cells(values: LedgerRowValues, palette: LedgerStylePalette) -> dict[str, Text]:
    """Build Rich cells for the retry activation row."""
    return {
        COLUMN_POSITION: Text(values[COLUMN_POSITION], style=palette.warning + Style(bold=True)),
        COLUMN_EVENT: Text(values[COLUMN_EVENT], style=palette.warning + Style(bold=True)),
        COLUMN_SUMMARY: Text(values[COLUMN_SUMMARY], style=palette.warning),
        COLUMN_STATUS: Text(values[COLUMN_STATUS], style=palette.retry),
    }


def entry_cells(
    entry: LedgerEntry,
    values: LedgerRowValues,
    *,
    record: TrajectoryRecord | None,
    request: TrajectoryRequest | None,
    tool: TrajectoryToolOperation | None,
    palette: LedgerStylePalette,
    record_hovered: bool,
    entry_hovered: bool,
    duration_mode: bool,
) -> Mapping[str, Text | str] | None:
    """Build Rich cells for one canonical entry projection."""
    if entry.is_request_header:
        return request_cells(request, values, palette)
    if entry.is_group_header:
        return group_cells(values)
    if record is None:
        return None
    if entry.is_tool_operation and tool is not None:
        return tool_cells(tool, values, palette, hovered=entry_hovered)
    return record_cells(
        record, values, palette, hovered=record_hovered, duration_mode=duration_mode
    )


__all__ = [
    "COLUMN_DURATION",
    "COLUMN_EVENT",
    "COLUMN_KEYS",
    "COLUMN_LABELS",
    "COLUMN_POSITION",
    "COLUMN_SOURCE",
    "COLUMN_STATUS",
    "COLUMN_SUMMARY",
    "LedgerRowValues",
    "LedgerStylePalette",
    "empty_cells",
    "empty_values",
    "entry_cells",
    "entry_values",
    "group_cells",
    "group_values",
    "history_cells",
    "history_values",
    "lane_style",
    "record_cells",
    "record_values",
    "request_cells",
    "request_values",
    "retry_cells",
    "retry_values",
    "status_style",
    "tool_cells",
    "tool_values",
]
