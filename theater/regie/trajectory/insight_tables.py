"""Pure table models for trajectory diagnostic projections."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from rich.text import Text

from theater.constants.regie_trajectory import (
    TRAJECTORY_AUXILIARY_ROW_HEIGHT,
    TRAJECTORY_INSIGHT_ROW_LIMIT,
    TRAJECTORY_RESOURCE_HEAT_GLYPH,
    TRAJECTORY_RESOURCE_HEAT_WIDTH,
    TRAJECTORY_SPAN_ROW_HEIGHT,
    WATERFALL_BAR_WIDTH,
)
from theater.regie.trajectory.analysis import (
    ProblemActivity,
    ResourceValues,
    TrajectoryAnalysisIndex,
    WaterfallProjection,
    WaterfallRow,
)
from theater.regie.trajectory.analysis.waterfall import timing_interval
from theater.regie.trajectory.enums import DiagnosticView
from theater.regie.trajectory.render import (
    compact_cost,
    compact_number,
    format_duration,
    sanitize_text,
)
from theater.trajectory import ParticipantLink, TrajectoryStatus


@dataclass(frozen=True, slots=True)
class InsightColumn:
    key: str
    label: str
    width: int | None = None


@dataclass(frozen=True, slots=True)
class InsightEntry:
    key: str
    record_id: str | None
    member_record_ids: tuple[str, ...]
    cells: tuple[Text | str, ...]
    link: ParticipantLink | None = None
    row_height: int | None = None


@dataclass(frozen=True, slots=True)
class InsightTableModel:
    columns: tuple[InsightColumn, ...]
    entries: tuple[InsightEntry, ...]
    empty_message: str
    row_height: int = TRAJECTORY_SPAN_ROW_HEIGHT


def _clip(value: str, limit: int) -> str:
    clean = " ".join(sanitize_text(value).replace("\r", " ").splitlines()).strip()
    return clean if len(clean) <= limit else f"{clean[: limit - 1].rstrip()}…"


def _status(value: TrajectoryStatus) -> Text:
    style = (
        "red dim"
        if value in {TrajectoryStatus.ERROR, TrajectoryStatus.TIMEOUT}
        else "yellow dim"
        if value
        in {
            TrajectoryStatus.CANCELLED,
            TrajectoryStatus.INTERRUPTED,
            TrajectoryStatus.PARTIAL,
            TrajectoryStatus.PENDING,
        }
        else "cyan dim"
        if value is TrajectoryStatus.RUNNING
        else "dim"
        if value is TrajectoryStatus.UNKNOWN
        else "green dim"
    )
    return Text(value.value.replace("_", " "), style=style, no_wrap=True)


def _waterfall_bar(row: WaterfallRow, request: WaterfallProjection) -> Text:
    interval = timing_interval(row.timing)
    if interval is None or request.start is None or request.end is None:
        return Text("timing unavailable", style="dim", no_wrap=True)
    span = max(request.end - request.start, 1e-9)
    start = round((interval[0] - request.start) / span * (WATERFALL_BAR_WIDTH - 1))
    end = round((interval[1] - request.start) / span * (WATERFALL_BAR_WIDTH - 1))
    start = max(0, min(WATERFALL_BAR_WIDTH - 1, start))
    end = max(start, min(WATERFALL_BAR_WIDTH - 1, end))
    cells = [" " for _ in range(WATERFALL_BAR_WIDTH)]
    for index in range(start, end + 1):
        cells[index] = "━"
    cells[start] = "╺" if start < end else "◆"
    if start < end:
        cells[end] = "╸"
    if row.request and request.first_token is not None:
        token = round((request.first_token - request.start) / span * (WATERFALL_BAR_WIDTH - 1))
        if 0 <= token < WATERFALL_BAR_WIDTH:
            cells[token] = "◆"
    return Text(
        "".join(cells),
        style="cyan dim" if row.request else "yellow dim",
        no_wrap=True,
    )


def _waterfall_entries(
    index: TrajectoryAnalysisIndex, visible: frozenset[str]
) -> tuple[InsightEntry, ...]:
    blocks: deque[tuple[InsightEntry, ...]] = deque()
    total = 0
    for request in index.waterfalls:
        matching = [row for row in request.rows if visible.intersection(row.member_record_ids)]
        if not matching:
            continue
        entries: list[InsightEntry] = []
        for row in (row for row in request.rows if row.request or row in matching):
            indent = "  " * min(row.depth, 8)
            label = f"{indent}{'◆' if row.request else '↳'} {_clip(row.label, 30)}"
            entries.append(
                InsightEntry(
                    key=f"waterfall:{row.key}",
                    record_id=row.record_id,
                    member_record_ids=row.member_record_ids,
                    cells=(
                        "request" if row.request else f"tool {row.depth}",
                        Text(label, style="cyan dim" if row.request else "yellow dim"),
                        _waterfall_bar(row, request),
                        format_duration(row.timing),
                        _status(row.status),
                    ),
                    row_height=TRAJECTORY_SPAN_ROW_HEIGHT,
                )
            )
        block = tuple(entries)
        blocks.append(block)
        total += len(block)
        while blocks and total > TRAJECTORY_INSIGHT_ROW_LIMIT:
            total -= len(blocks.popleft())
    return tuple(entry for block in blocks for entry in block)


def _file_entries(
    index: TrajectoryAnalysisIndex, visible: frozenset[str]
) -> tuple[InsightEntry, ...]:
    entries: list[InsightEntry] = []
    for row in index.files:
        if not visible.intersection(row.record_ids):
            continue
        modes = "/".join(mode for mode in ("read", "write", "reference") if mode in row.modes)
        entries.append(
            InsightEntry(
                key=f"file:{row.path}",
                record_id=None,
                member_record_ids=row.record_ids,
                cells=(
                    modes or "reference",
                    Text(_clip(row.path, 72), style="cyan dim", no_wrap=True),
                    f"{row.operation_count} operations",
                    "—",
                    _status(row.status),
                ),
                row_height=TRAJECTORY_AUXILIARY_ROW_HEIGHT,
            )
        )
        for position, operation in enumerate(row.operations, start=1):
            operation_modes = "/".join(
                mode for mode in ("read", "write", "reference") if mode in operation.modes
            )
            branch = "└─" if position == row.operation_count else "├─"
            entries.append(
                InsightEntry(
                    key=f"file:{row.path}:operation:{operation.operation_id}",
                    record_id=operation.record_id,
                    member_record_ids=operation.record_ids,
                    cells=(
                        operation_modes or "reference",
                        Text(
                            f"  {branch} {_clip(operation.tool_name or 'unknown tool', 60)}",
                            style="yellow dim",
                            no_wrap=True,
                        ),
                        f"#{position}",
                        format_duration(operation.timing),
                        _status(operation.status),
                    ),
                    row_height=TRAJECTORY_SPAN_ROW_HEIGHT,
                )
            )
    return tuple(entries)


def _delegation_entries(
    index: TrajectoryAnalysisIndex, visible: frozenset[str]
) -> tuple[InsightEntry, ...]:
    entries = []
    for row in index.delegations:
        if not visible.intersection(row.record_ids):
            continue
        incoming = any(direction.value == "incoming" for direction in row.directions)
        outgoing = any(direction.value == "outgoing" for direction in row.directions)
        flow = "↔" if incoming and outgoing else "←" if incoming else "→" if outgoing else "◇"
        fidelity = "exact" if row.target.target_record_id is not None else "participant"
        entries.append(
            InsightEntry(
                key=f"delegation:{row.participant_id}",
                record_id=row.latest_record_id,
                member_record_ids=row.record_ids,
                cells=(
                    Text(flow, style="magenta dim"),
                    _clip(row.participant_id, 28),
                    _clip(", ".join(row.relations), 28),
                    str(row.event_count),
                    _clip(row.latest_summary, 48),
                    fidelity,
                    _status(row.latest_status),
                ),
                link=row.target,
            )
        )
    return tuple(entries)


def _heat(value: int | float, maximum: int | float, *, style: str = "cyan dim") -> Text:
    filled = 0 if maximum <= 0 else round(value / maximum * TRAJECTORY_RESOURCE_HEAT_WIDTH)
    filled = max(0, min(TRAJECTORY_RESOURCE_HEAT_WIDTH, filled))
    return Text(
        TRAJECTORY_RESOURCE_HEAT_GLYPH * filled + "·" * (TRAJECTORY_RESOURCE_HEAT_WIDTH - filled),
        style=style,
        no_wrap=True,
    )


def _cost(values: ResourceValues) -> str:
    if values.cost_usd is None:
        return "— unknown"
    prefix = "~" if values.cost_provenance in {"estimated", "mixed"} else ""
    suffix = " +?" if not values.cost_complete else ""
    return f"{prefix}${compact_cost(values.cost_usd)} {values.cost_provenance}{suffix}"


def _resource_entries(
    index: TrajectoryAnalysisIndex, visible: frozenset[str]
) -> tuple[InsightEntry, ...]:
    rows = [row for row in index.resources if visible.intersection(row.record_ids)]
    maximum_tokens = max((row.values.total_tokens for row in rows), default=0)
    maximum_cost = max(
        (row.values.cost_usd for row in rows if row.values.cost_usd is not None),
        default=0.0,
    )
    return tuple(
        InsightEntry(
            key=f"resource:{row.key}",
            record_id=row.record_id,
            member_record_ids=row.record_ids,
            cells=(
                f"{'  ' * row.depth}{row.scope}",
                _clip(row.model or row.label, 30),
                _heat(row.values.total_tokens, maximum_tokens),
                _heat(row.values.cost_usd, maximum_cost, style="magenta dim")
                if row.values.cost_usd is not None
                else Text("unknown", style="yellow dim", no_wrap=True),
                compact_number(row.values.input_tokens),
                compact_number(row.values.output_tokens),
                compact_number(row.values.cache_tokens),
                compact_number(row.values.reasoning_tokens),
                _cost(row.values),
            ),
            row_height=(
                TRAJECTORY_AUXILIARY_ROW_HEIGHT
                if row.scope == "turn"
                else TRAJECTORY_SPAN_ROW_HEIGHT
            ),
        )
        for row in rows
    )


def _problem_label(row: ProblemActivity) -> Text:
    prefix = "  " * min(row.chain_depth, 8)
    glyph = "↻" if row.retry_of_record_id is not None else "!"
    return Text(f"{prefix}{glyph} {_clip(row.label, 36)}", style="red dim", no_wrap=True)


def _problem_entries(
    index: TrajectoryAnalysisIndex, visible: frozenset[str]
) -> tuple[InsightEntry, ...]:
    entries = []
    for row in index.problems:
        if not visible.intersection(row.member_record_ids):
            continue
        category = row.failure.category.value if row.failure is not None else "unknown"
        code = row.failure.code if row.failure is not None and row.failure.code else "—"
        retry = (
            f"attempt {row.retry_attempt or '?'} → {_clip(row.retry_of_record_id, 18)}"
            if row.retry_of_record_id
            else "—"
        )
        detail = row.failure.detail if row.failure is not None else ""
        entries.append(
            InsightEntry(
                key=f"problem:{row.record_id}",
                record_id=row.record_id,
                member_record_ids=row.member_record_ids,
                cells=(
                    _problem_label(row),
                    category.replace("_", " "),
                    _clip(code, 24),
                    _clip(detail or "no failure detail", 48),
                    _status(row.status),
                    retry,
                ),
            )
        )
    return tuple(entries)


def build_insight_table(
    view: DiagnosticView,
    index: TrajectoryAnalysisIndex,
    visible_ids: frozenset[str],
) -> InsightTableModel:
    """Build one bounded display model from cached analysis."""
    if view is DiagnosticView.WATERFALL:
        return InsightTableModel(
            (
                InsightColumn("scope", "SCOPE", 9),
                InsightColumn("operation", "REQUEST / TOOL", 32),
                InsightColumn("waterfall", "WATERFALL", WATERFALL_BAR_WIDTH),
                InsightColumn("time", "TIME", 10),
                InsightColumn("state", "STATE", 12),
            ),
            _waterfall_entries(index, visible_ids),
            "No request timing in the loaded scope",
        )
    if view is DiagnosticView.FILES:
        return InsightTableModel(
            (
                InsightColumn("mode", "MODE", 14),
                InsightColumn("activity", "FILE / OPERATION", 50),
                InsightColumn("order", "ORDER", 12),
                InsightColumn("time", "TIME", 10),
                InsightColumn("state", "STATE", 12),
            ),
            _file_entries(index, visible_ids),
            "No structured file activity in the loaded scope",
        )
    if view is DiagnosticView.DELEGATION:
        return InsightTableModel(
            (
                InsightColumn("flow", "FLOW", 6),
                InsightColumn("participant", "PARTICIPANT", 28),
                InsightColumn("relation", "RELATION", 24),
                InsightColumn("events", "EVENTS", 7),
                InsightColumn("latest", "LATEST ACTIVITY", 42),
                InsightColumn("link", "LINK", 12),
                InsightColumn("state", "STATE", 12),
            ),
            _delegation_entries(index, visible_ids),
            "No cross-participant activity in the loaded scope",
            row_height=TRAJECTORY_AUXILIARY_ROW_HEIGHT,
        )
    if view is DiagnosticView.RESOURCES:
        return InsightTableModel(
            (
                InsightColumn("scope", "SCOPE", 10),
                InsightColumn("model", "MODEL / TURN", 30),
                InsightColumn("token_heat", "TOKENS", TRAJECTORY_RESOURCE_HEAT_WIDTH),
                InsightColumn("cost_heat", "$ LOAD", TRAJECTORY_RESOURCE_HEAT_WIDTH),
                InsightColumn("input", "IN", 8),
                InsightColumn("output", "OUT", 8),
                InsightColumn("cache", "CACHE", 8),
                InsightColumn("reason", "REASON", 8),
                InsightColumn("cost", "COST", 22),
            ),
            _resource_entries(index, visible_ids),
            "No token or cost usage in the loaded scope",
        )
    return InsightTableModel(
        (
            InsightColumn("operation", "FAILURE / RETRY", 38),
            InsightColumn("category", "CATEGORY", 20),
            InsightColumn("code", "CODE", 24),
            InsightColumn("detail", "DETAIL", 42),
            InsightColumn("state", "STATE", 12),
            InsightColumn("retry", "RETRY OF", 26),
        ),
        _problem_entries(index, visible_ids),
        "No failures or retries in the loaded scope",
    )


__all__ = [
    "InsightColumn",
    "InsightEntry",
    "InsightTableModel",
    "build_insight_table",
]
