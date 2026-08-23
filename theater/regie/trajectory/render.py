"""Pure bounded render helpers shared by trajectory widgets."""

from __future__ import annotations

import json

from rich.text import Text

from theater.regie.trajectory.constants import (
    KIND_GLYPHS_BY_VALUE,
    LANE_GLYPHS_BY_VALUE,
    MAX_TOOLTIP_BYTES,
    STYLE_DURATION,
    STYLE_MATCHED,
    TOOLTIP_DELAY,
    TRAJECTORY_DETAIL_RECORD_MAX_BYTES,
)
from theater.regie.trajectory.enums import InspectorTab
from theater.trajectory import (
    ContentFormat,
    DetailField,
    ParticipantLink,
    Timing,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
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
    milliseconds = timing.duration_ms
    if milliseconds < 1_000:
        return f"{milliseconds:g}ms"
    seconds = milliseconds / 1_000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remainder = divmod(seconds, 60)
    return f"{int(minutes)}m {remainder:.0f}s"


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


def group_line(label: str, *, collapsed: bool, depth: int = 0) -> Text:
    glyph = "▸" if collapsed else "▾"
    return Text(f"{'  ' * depth}{glyph} {_compact(label, 120)}", no_wrap=True, overflow="crop")


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
    return f"participant {direction}: {link.participant_id} ({link.relation})"


def _inspector_lines(  # noqa: PLR0912
    record: TrajectoryRecord | None, tab: InspectorTab
) -> tuple[list[str], dict[int, str]]:
    if record is None:
        return ["No record selected."], {}
    lines = [
        f"{record.kind.value} · {record.source} · {status_label(record.status)}",
    ]
    lines.extend(record.summary.split("\n"))
    link_lines: dict[int, str] = {}

    def append_fields(fields: tuple[DetailField, ...]) -> None:
        for field in fields:
            values = _format_detail(field).split("\n")
            lines.append(f"{field.name}: {values[0]}")
            lines.extend(values[1:])

    if tab is InspectorTab.SUMMARY:
        append_fields(record.details[:8])
    elif tab in {
        InspectorTab.OUTPUT,
        InspectorTab.RESULT,
        InspectorTab.PAYLOAD,
        InspectorTab.RAW,
        InspectorTab.INPUT,
        InspectorTab.REASONING,
        InspectorTab.PREVIEW,
    }:
        fields = _fields_for_tab(record, tab)
        if fields:
            append_fields(fields)
        elif tab not in {InspectorTab.SUMMARY}:
            lines.append(f"No {tab.value} supplied.")
    elif tab in {InspectorTab.CURRENT, InspectorTab.PREVIOUS, InspectorTab.DIFF}:
        fields = _fields_for_tab(record, tab)
        if fields:
            append_fields(fields)
        else:
            lines.append(f"No {tab.value} context supplied.")
    elif tab is InspectorTab.USAGE:
        if record.usage is not None:
            lines.append(
                json.dumps(record.usage.to_wire(), ensure_ascii=False, indent=2, sort_keys=True)
            )
        else:
            lines.append("No usage recorded.")
    elif tab is InspectorTab.TIMING:
        lines.append(f"Duration: {format_duration(record.timing)}")
        if record.timing is not None:
            lines.extend(
                (
                    f"Start: {record.timing.start}",
                    f"End: {record.timing.end}",
                    f"Provenance: {record.timing.provenance.value}",
                )
            )
    elif tab is InspectorTab.SOURCE:
        lines.extend((f"Source: {record.source}", f"Epoch: {record.source_epoch}"))

    if tab not in {InspectorTab.USAGE, InspectorTab.TIMING, InspectorTab.SOURCE}:
        for link in record.links:
            lines.append(_participant_line(link))
            link_lines[len(lines) - 1] = link.participant_id
    return lines, link_lines


def _bounded_lines(lines: list[str]) -> str:
    return bounded_preview(
        "\n".join(plain_text(line) for line in lines),
        max_bytes=TRAJECTORY_DETAIL_RECORD_MAX_BYTES,
    ).text


def inspector_text(record: TrajectoryRecord | None, tab: InspectorTab) -> str:
    """Build the exact bounded text exposed by the active inspector tab."""
    lines, _ = _inspector_lines(record, tab)
    return _bounded_lines(lines)


def inspector_link_line_ids(record: TrajectoryRecord | None, tab: InspectorTab) -> dict[int, str]:
    lines, links = _inspector_lines(record, tab)
    bounded = _bounded_lines(lines).splitlines()
    return {
        line_index: participant_id
        for line_index, participant_id in links.items()
        if line_index < len(bounded) and bounded[line_index] == plain_text(lines[line_index])
    }


def inspector_content(record: TrajectoryRecord | None, tab: InspectorTab) -> Text:
    """Return displayed inspector content without Rich markup interpretation."""
    return Text(inspector_text(record, tab), no_wrap=False)


def tooltip_text(record: TrajectoryRecord) -> str:
    """Return a small bounded hover detail."""
    return bounded_preview(
        (
            f"{record.kind.value} · {record.source}\n"
            f"{record.summary}\n{format_duration(record.timing)}"
        ),
        max_bytes=MAX_TOOLTIP_BYTES,
    ).text


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
    "count_label",
    "details_size",
    "format_duration",
    "group_line",
    "inspector_content",
    "inspector_link_line_ids",
    "inspector_text",
    "kind_glyph",
    "lane_glyph",
    "plain_text",
    "record_line",
    "sanitize_text",
    "status_label",
    "tabs_for_record",
    "tooltip_text",
]
