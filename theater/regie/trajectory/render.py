"""Pure bounded render helpers shared by trajectory widgets."""

from __future__ import annotations

import json

from rich.cells import cell_len, set_cell_size
from rich.text import Text

from theater.regie.trajectory.constants import (
    KIND_GLYPHS_BY_VALUE,
    LANE_GLYPHS_BY_VALUE,
    STYLE_DURATION,
    STYLE_MATCHED,
    TOOLTIP_DELAY,
    TRAJECTORY_DETAIL_RECORD_MAX_BYTES,
    TRAJECTORY_OVERVIEW_COMPACT_NUMBER_THRESHOLD,
    TRAJECTORY_TOOLTIP_SUMMARY_MAX_CELLS,
)
from theater.regie.trajectory.enums import InspectorTab
from theater.regie.trajectory.inspector import (
    InspectorLine,
    failure_lines,
    request_association_lines,
    request_summary_lines,
    request_timing_lines,
    request_usage_lines,
    retry_lines,
)
from theater.trajectory import (
    ContentFormat,
    DetailField,
    ParticipantLink,
    Timing,
    TimingProvenance,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryRequest,
    TrajectoryStatus,
    bounded_preview,
)
from theater.trajectory import (
    sanitize_text as _sanitize_text,
)

LANE_GLYPHS = {TrajectoryLane(value): glyph for value, glyph in LANE_GLYPHS_BY_VALUE.items()}
KIND_GLYPHS = {TrajectoryKind(value): glyph for value, glyph in KIND_GLYPHS_BY_VALUE.items()}


def sanitize_text(value: str) -> str:
    """Make terminal controls visible while preserving literal brackets and slashes."""
    return _sanitize_text(value)


def plain_text(value: str) -> str:
    """Return safe plain text for bounded copy."""
    return sanitize_text(value)


def _compact(value: str, limit: int) -> str:
    return sanitize_text(value).replace("\r", " ").replace("\n", " ")[:limit]


def lane_glyph(lane: TrajectoryLane) -> str:
    return LANE_GLYPHS.get(lane, "?")


def kind_glyph(kind: TrajectoryKind) -> str:
    return KIND_GLYPHS.get(kind, "?")


def status_label(status: TrajectoryStatus) -> str:
    return status.value.replace("_", " ")


def format_duration(timing: Timing | None) -> str:
    if timing is None or timing.duration_ms is None:
        return "—"
    return _format_milliseconds(timing.duration_ms)


def _format_milliseconds(milliseconds: float) -> str:
    if milliseconds < 1_000:
        return f"{milliseconds:g}ms"
    seconds = milliseconds / 1_000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remainder = divmod(seconds, 60)
    return f"{int(minutes)}m {remainder:.0f}s"


def compact_number(value: int) -> str:
    """Render a non-negative count using the overview's compact notation."""
    if value < TRAJECTORY_OVERVIEW_COMPACT_NUMBER_THRESHOLD:
        return str(value)
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if value >= divisor:
            return f"{value / divisor:.1f}".rstrip("0").rstrip(".") + suffix
    return str(value)


def compact_cost(value: float) -> str:
    """Render a reported dollar value using the overview's compact precision."""
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


def supports_duration_interval(record: TrajectoryRecord) -> bool:
    """Whether a record has independently reported usable interval data."""
    timing = record.timing
    return (
        timing is not None
        and timing.provenance
        in {
            TimingProvenance.SOURCE,
            TimingProvenance.OBSERVED,
        }
        and (
            timing.duration_ms is not None or (timing.start is not None and timing.end is not None)
        )
    )


def record_line(
    record: TrajectoryRecord,
    index: int,
    *,
    selected: bool = False,
    hovered: bool = False,
    duration_mode: bool = False,
    depth: int = 0,
) -> Text:
    """Render one compact, non-wrapping ledger line."""
    marker = "▶" if selected else ("·" if hovered else " ")
    duration = format_duration(record.timing)
    position = f"{index:>4}" if not duration_mode else f"dur {duration:>7}"
    indent = "  " * depth
    line = Text(no_wrap=True, overflow="crop")
    line.append(f"{indent}{marker} {position} ")
    line.append(f"{kind_glyph(record.kind)} ")
    line.append(f"{_compact(record.source, 16):<16} ")
    line.append(f"{_compact(record.summary, 64)} ")
    line.append(f"{status_label(record.status):<11} ")
    line.append(duration, style=STYLE_DURATION if duration_mode else STYLE_MATCHED)
    return line


def tabs_for_record(record: TrajectoryRecord | None) -> tuple[InspectorTab, ...]:
    if record is None:
        return (InspectorTab.SUMMARY,)
    if record.kind in {TrajectoryKind.SYSTEM, TrajectoryKind.CONTEXT}:
        return (InspectorTab.CURRENT, InspectorTab.PREVIOUS, InspectorTab.DIFF)
    if (
        record.kind
        in {
            TrajectoryKind.ASSISTANT,
            TrajectoryKind.REASONING,
        }
        or record.lane is TrajectoryLane.MODEL
    ):
        return (
            InspectorTab.SUMMARY,
            InspectorTab.OUTPUT,
            InspectorTab.REASONING,
            InspectorTab.USAGE,
            InspectorTab.TIMING,
            InspectorTab.ASSOCIATIONS,
        )
    if (
        record.kind
        in {
            TrajectoryKind.TOOL_CALL,
            TrajectoryKind.TOOL_RESULT,
        }
        or record.lane is TrajectoryLane.TOOLS
    ):
        return (InspectorTab.SUMMARY, InspectorTab.INPUT, InspectorTab.RESULT, InspectorTab.TIMING)
    if record.kind is TrajectoryKind.USER or record.lane is TrajectoryLane.INPUT:
        return (InspectorTab.PREVIEW, InspectorTab.RAW, InspectorTab.SOURCE)
    if record.lane is TrajectoryLane.THEATER:
        return (InspectorTab.SUMMARY, InspectorTab.PAYLOAD, InspectorTab.TIMING)
    return (InspectorTab.SUMMARY, InspectorTab.TIMING)


_TAB_FIELD_ALIASES: dict[InspectorTab, frozenset[str]] = {
    InspectorTab.OUTPUT: frozenset({"assistant_output", "content", "output", "response", "text"}),
    InspectorTab.REASONING: frozenset({"reasoning", "reasoning_content", "reasoning_summary"}),
    InspectorTab.INPUT: frozenset({"args", "arguments", "input", "parameters", "tool_input"}),
    InspectorTab.RESULT: frozenset({"output", "response", "result", "tool_result"}),
    InspectorTab.PREVIEW: frozenset({"content", "input", "preview", "prompt", "text"}),
    InspectorTab.RAW: frozenset({"raw", "raw_text", "source_text", "transcript"}),
    InspectorTab.PAYLOAD: frozenset({"data", "event", "message", "payload"}),
    InspectorTab.CURRENT: frozenset({"current", "context_current", "current_context", "state"}),
    InspectorTab.PREVIOUS: frozenset(
        {"previous", "context_previous", "previous_context", "previous_state"}
    ),
    InspectorTab.DIFF: frozenset({"context_diff", "diff", "changes"}),
}


def _field_key(name: str) -> str:
    return name.casefold().replace("-", "_").replace(" ", "_")


def _fields_for_tab(record: TrajectoryRecord, tab: InspectorTab) -> tuple[DetailField, ...]:
    aliases = _TAB_FIELD_ALIASES.get(tab, frozenset())
    return tuple(field for field in record.details if _field_key(field.name) in aliases)


def _format_detail(field: DetailField) -> str:
    preview = field.preview
    if field.format in {ContentFormat.IMAGE, ContentFormat.BINARY}:
        total = preview.encoded_bytes + preview.omitted_bytes
        return f"{field.format.value} metadata ({total} bytes)"
    if field.format is ContentFormat.JSON:
        try:
            return json.dumps(
                json.loads(preview.text), ensure_ascii=False, indent=2, sort_keys=True
            )
        except (TypeError, ValueError):
            return plain_text(preview.text)
    return plain_text(preview.text)


def _participant_line(link: ParticipantLink) -> str:
    direction = link.direction.value
    target = (
        f" · exact target {link.target_record_id}"
        if link.target_record_id is not None
        else " · participant only"
    )
    return f"participant {direction}: {link.participant_id} ({link.relation}){target}"


def _field_lines(fields: tuple[DetailField, ...]) -> tuple[InspectorLine, ...]:
    lines: list[InspectorLine] = []
    for field in fields:
        values = _format_detail(field).split("\n")
        lines.append(InspectorLine(f"{field.name}: {values[0]}"))
        lines.extend(InspectorLine(value) for value in values[1:])
    return tuple(lines)


def _record_tab_lines(  # noqa: PLR0912
    record: TrajectoryRecord,
    tab: InspectorTab,
    request: TrajectoryRequest | None,
) -> tuple[InspectorLine, ...]:
    if tab is InspectorTab.SUMMARY:
        lines = [*_field_lines(record.details[:8])]
        if request is not None:
            lines.extend(request_summary_lines(request))
        else:
            lines.extend(failure_lines(record.failure))
            lines.extend(retry_lines(record.retry_of_record_id, record.retry_attempt))
        return tuple(lines)
    if tab in {
        InspectorTab.OUTPUT,
        InspectorTab.RESULT,
        InspectorTab.PAYLOAD,
        InspectorTab.RAW,
        InspectorTab.INPUT,
        InspectorTab.REASONING,
        InspectorTab.PREVIEW,
    }:
        fields = _fields_for_tab(record, tab)
        return _field_lines(fields) if fields else (InspectorLine(f"No {tab.value} supplied."),)
    if tab in {InspectorTab.CURRENT, InspectorTab.PREVIOUS, InspectorTab.DIFF}:
        fields = _fields_for_tab(record, tab)
        return (
            _field_lines(fields)
            if fields
            else (InspectorLine(f"No {tab.value} context supplied."),)
        )
    if tab is InspectorTab.USAGE:
        if request is not None:
            return request_usage_lines(request)
        if record.usage is not None:
            return (
                InspectorLine(
                    json.dumps(record.usage.to_wire(), ensure_ascii=False, indent=2, sort_keys=True)
                ),
            )
        return (InspectorLine("No usage recorded."),)
    if tab is InspectorTab.TIMING:
        if request is not None:
            return request_timing_lines(request)
        lines = [InspectorLine(f"Duration: {format_duration(record.timing)}")]
        if record.timing is not None:
            lines.extend(
                InspectorLine(value)
                for value in (
                    f"Start: {record.timing.start}",
                    f"End: {record.timing.end}",
                    f"Provenance: {record.timing.provenance.value}",
                )
            )
        return tuple(lines)
    if tab is InspectorTab.ASSOCIATIONS:
        return request_association_lines(request)
    if tab is InspectorTab.SOURCE:
        return (
            InspectorLine(f"Source: {record.source}"),
            InspectorLine(f"Epoch: {record.source_epoch}"),
        )
    return ()


def _detail_lines(
    record: TrajectoryRecord | None,
    tab: InspectorTab,
    request: TrajectoryRequest | None = None,
) -> tuple[list[str], dict[int, ParticipantLink], dict[int, str]]:
    if record is None:
        return ["No record selected."], {}, {}
    lines = [
        f"{record.kind.value} · {record.source} · {status_label(record.status)}",
        *record.summary.split("\n"),
    ]
    record_link_lines: dict[int, str] = {}
    for value in _record_tab_lines(record, tab, request):
        lines.append(value.text)
        if value.target_record_id is not None:
            record_link_lines[len(lines) - 1] = value.target_record_id

    link_lines: dict[int, ParticipantLink] = {}
    if tab not in {InspectorTab.USAGE, InspectorTab.TIMING, InspectorTab.SOURCE}:
        for link in record.links:
            lines.append(_participant_line(link))
            link_lines[len(lines) - 1] = link
    return lines, link_lines, record_link_lines


def _bounded_lines(lines: list[str]) -> str:
    return bounded_preview(
        "\n".join(plain_text(line) for line in lines),
        max_bytes=TRAJECTORY_DETAIL_RECORD_MAX_BYTES,
    ).text


def detail_text(
    record: TrajectoryRecord | None,
    tab: InspectorTab,
    request: TrajectoryRequest | None = None,
) -> str:
    """Build the exact bounded text exposed by the active detail tab."""
    lines, _, _ = _detail_lines(record, tab, request)
    return _bounded_lines(lines)


def detail_link_line_ids(record: TrajectoryRecord | None, tab: InspectorTab) -> dict[int, str]:
    return {
        line_index: link.participant_id
        for line_index, link in detail_links_by_line(record, tab).items()
    }


def detail_links_by_line(
    record: TrajectoryRecord | None,
    tab: InspectorTab,
    request: TrajectoryRequest | None = None,
) -> dict[int, ParticipantLink]:
    lines, links, _ = _detail_lines(record, tab, request)
    bounded = _bounded_lines(lines).splitlines()
    return {
        line_index: link
        for line_index, link in links.items()
        if line_index < len(bounded) and bounded[line_index] == plain_text(lines[line_index])
    }


def detail_record_links_by_line(
    record: TrajectoryRecord | None,
    tab: InspectorTab,
    request: TrajectoryRequest | None = None,
) -> dict[int, str]:
    lines, _, links = _detail_lines(record, tab, request)
    bounded = _bounded_lines(lines).splitlines()
    return {
        line_index: record_id
        for line_index, record_id in links.items()
        if line_index < len(bounded) and bounded[line_index] == plain_text(lines[line_index])
    }


def tooltip_text(record: TrajectoryRecord) -> str:
    """Return bounded, type-aware hover detail."""
    usage = record.usage
    identity = record.source
    if usage is not None and usage.model and usage.model != identity:
        identity = f"{identity} · {usage.model}"
    elif record.kind is TrajectoryKind.TOOL_CALL and record.summary:
        identity = f"{identity} · {' '.join(plain_text(record.summary).splitlines())}"
    kind = record.kind.value.replace("_", " ").upper()
    heading = f"{kind_glyph(record.kind)} {kind} · {identity} · {status_label(record.status)}"
    summary = " ".join(plain_text(record.summary).splitlines()) or "No preview available"
    metrics: list[str] = []
    timing = record.timing
    duration = format_duration(timing)
    if duration != "—":
        metrics.append(f"total {duration}")
    if timing is not None and timing.ttft_ms is not None:
        metrics.append(f"TTFT {_format_milliseconds(timing.ttft_ms)}")
    if timing is not None and timing.generation_duration_ms is not None:
        metrics.append(f"generation {_format_milliseconds(timing.generation_duration_ms)}")
    if usage is not None:
        metrics.extend(
            (
                f"in {compact_number(usage.input_tokens)}",
                f"out {compact_number(usage.output_tokens)}",
            )
        )
        if usage.cost_usd is not None:
            metrics.append(f"cost ${compact_cost(usage.cost_usd)}")
    detail = " · ".join(metrics) or "timing unavailable"
    return "\n".join(_bounded_tooltip_line(line) for line in (heading, summary, detail))


def _bounded_tooltip_line(value: str) -> str:
    value = sanitize_text(value).replace("\r", " ").replace("\n", " ")
    if cell_len(value) <= TRAJECTORY_TOOLTIP_SUMMARY_MAX_CELLS:
        return value
    return set_cell_size(value, TRAJECTORY_TOOLTIP_SUMMARY_MAX_CELLS - 1).rstrip() + "…"


def count_label(value: str, count: int) -> str:
    return f"{value} ({count})"


def details_size(record: TrajectoryRecord) -> int:
    return sum(
        len(field.name.encode("utf-8")) + field.preview.encoded_bytes for field in record.details
    )


__all__ = [
    "KIND_GLYPHS",
    "LANE_GLYPHS",
    "TOOLTIP_DELAY",
    "compact_cost",
    "compact_number",
    "count_label",
    "detail_link_line_ids",
    "detail_links_by_line",
    "detail_record_links_by_line",
    "detail_text",
    "details_size",
    "format_duration",
    "kind_glyph",
    "lane_glyph",
    "plain_text",
    "record_line",
    "sanitize_text",
    "status_label",
    "tabs_for_record",
    "tooltip_text",
]
