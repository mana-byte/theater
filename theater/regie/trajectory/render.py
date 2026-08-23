"""Pure bounded render helpers shared by trajectory widgets."""

from __future__ import annotations

import json
import re

from rich.text import Text

from theater.regie.trajectory.constants import (
    KIND_GLYPHS_BY_VALUE,
    LANE_GLYPHS_BY_VALUE,
    MAX_DETAIL_BYTES,
    MAX_TOOLTIP_BYTES,
    STYLE_DURATION,
    STYLE_MATCHED,
    TOOLTIP_DELAY,
)
from theater.regie.trajectory.models import (
    ContentFormat,
    DetailField,
    InspectorTab,
    Lane,
    ParticipantLink,
    RecordKind,
    RecordStatus,
    Timing,
    TrajectoryRecord,
    clip_utf8,
)

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f]")
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")

LANE_GLYPHS = {Lane(value): glyph for value, glyph in LANE_GLYPHS_BY_VALUE.items()}
KIND_GLYPHS = {RecordKind(value): glyph for value, glyph in KIND_GLYPHS_BY_VALUE.items()}


def sanitize_text(value: str) -> str:
    """Remove terminal controls while preserving literal data characters."""
    return _CONTROL_RE.sub("�", _ANSI_RE.sub("", value))


def plain_text(value: str) -> str:
    """Return safe plain text for the bounded copy path."""
    return _CONTROL_RE.sub("�", _ANSI_RE.sub("", value))


def lane_glyph(lane: Lane) -> str:
    return LANE_GLYPHS.get(lane, "?")


def kind_glyph(kind: RecordKind) -> str:
    return KIND_GLYPHS.get(kind, "?")


def status_label(status: RecordStatus) -> str:
    return status.value.replace("_", " ")


def format_duration(timing: Timing | None) -> str:
    if timing is None or not timing.supports_duration or timing.duration_ms is None:
        return "—"
    milliseconds = timing.duration_ms
    if milliseconds < 1_000:
        return f"{milliseconds}ms"
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
) -> Text:
    """Render one compact ledger line without creating a row widget."""
    marker = "▶" if selected else ("·" if hovered else " ")
    duration = format_duration(record.timing)
    line = Text()
    position = f"{index:>4}" if not duration_mode else f"dur {duration:>7}"
    line.append(f"{marker} {position} ")
    line.append(f"{kind_glyph(record.kind)} ")
    line.append(f"{sanitize_text(record.source[:16]):<16} ")
    line.append(f"{sanitize_text(record.summary[:64])} ")
    line.append(f"{status_label(record.status):<11} ")
    line.append(duration, style=STYLE_DURATION if duration_mode else STYLE_MATCHED)
    return line


def group_line(label: str, *, collapsed: bool) -> Text:
    glyph = "▸" if collapsed else "▾"
    return Text(f"{glyph} {sanitize_text(label)}")


def tabs_for_record(record: TrajectoryRecord | None) -> tuple[InspectorTab, ...]:
    if record is None:
        return (InspectorTab.SUMMARY,)
    if record.kind in {RecordKind.SYSTEM, RecordKind.CONTEXT_CHANGE}:
        return (InspectorTab.CURRENT, InspectorTab.PREVIOUS, InspectorTab.DIFF)
    if record.kind in {RecordKind.ASSISTANT, RecordKind.REASONING} or record.lane == Lane.MODEL:
        return (
            InspectorTab.SUMMARY,
            InspectorTab.OUTPUT,
            InspectorTab.REASONING,
            InspectorTab.USAGE,
            InspectorTab.TIMING,
        )
    if record.kind in {RecordKind.TOOL_CALL, RecordKind.TOOL_RESULT} or record.lane == Lane.TOOLS:
        return (InspectorTab.SUMMARY, InspectorTab.INPUT, InspectorTab.RESULT, InspectorTab.TIMING)
    if record.kind in {RecordKind.USER} or record.lane == Lane.INPUT:
        return (InspectorTab.PREVIEW, InspectorTab.RAW, InspectorTab.SOURCE)
    if record.lane == Lane.THEATER:
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
    InspectorTab.USAGE: frozenset({"usage"}),
    InspectorTab.CURRENT: frozenset({"context_current", "current", "current_context", "state"}),
    InspectorTab.PREVIOUS: frozenset(
        {"context_previous", "previous", "previous_context", "previous_state"}
    ),
    InspectorTab.DIFF: frozenset({"context_diff", "diff", "changes"}),
}


def _field_key(name: str) -> str:
    return name.casefold().replace("-", "_").replace(" ", "_")


def _fields_for_tab(record: TrajectoryRecord, tab: InspectorTab) -> tuple[DetailField, ...]:
    aliases = _TAB_FIELD_ALIASES.get(tab, frozenset())
    return tuple(field for field in record.details if _field_key(field.name) in aliases)


def _format_detail(field: DetailField) -> str:
    value = field.value
    if value.format in {ContentFormat.IMAGE, ContentFormat.BINARY}:
        return f"{value.format.value} metadata ({value.original_bytes} bytes)"
    if value.format is ContentFormat.JSON:
        try:
            return json.dumps(json.loads(value.text), ensure_ascii=False, indent=2, sort_keys=True)
        except (TypeError, ValueError):
            return plain_text(value.text)
    return plain_text(value.text)


def _participant_line(link: ParticipantLink) -> str:
    return f"participant {link.direction.value}: {link.participant_id} {link.label or ''}".rstrip()


def _inspector_lines(  # noqa: PLR0912
    record: TrajectoryRecord | None, tab: InspectorTab
) -> tuple[list[str], list[tuple[int, str]]]:
    if record is None:
        return ["No record selected."], []
    lines = [
        f"{record.kind.value} · {record.source} · {status_label(record.status)}",
        record.summary,
    ]
    link_lines: list[tuple[int, str]] = []

    def append_fields(fields: tuple[DetailField, ...]) -> None:
        lines.extend(f"{field.name}: {_format_detail(field)}" for field in fields)

    if tab is InspectorTab.SUMMARY:
        append_fields(record.details[:8])
    elif tab in {
        InspectorTab.OUTPUT,
        InspectorTab.RESULT,
        InspectorTab.PAYLOAD,
        InspectorTab.RAW,
    }:
        fields = _fields_for_tab(record, tab)
        append_fields(fields or record.details)
    elif tab in {
        InspectorTab.INPUT,
        InspectorTab.REASONING,
        InspectorTab.PREVIEW,
        InspectorTab.CURRENT,
        InspectorTab.PREVIOUS,
        InspectorTab.DIFF,
    }:
        fields = _fields_for_tab(record, tab)
        if fields:
            append_fields(fields)
        elif tab is InspectorTab.CURRENT:
            lines.append("No current context supplied.")
        elif tab is InspectorTab.PREVIOUS:
            lines.append("No previous context supplied.")
        elif tab is InspectorTab.DIFF:
            lines.append("No context diff supplied.")
    elif tab is InspectorTab.USAGE:
        fields = _fields_for_tab(record, tab)
        if fields:
            append_fields(fields)
        else:
            lines.append(
                json.dumps(
                    record.usage.to_wire() if record.usage else "No usage recorded.",
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
    elif tab is InspectorTab.TIMING:
        lines.append(f"Duration: {format_duration(record.timing)}")
        if record.timing:
            lines.extend(
                value
                for value in (record.timing.started_at, record.timing.finished_at)
                if value is not None
            )
    elif tab is InspectorTab.SOURCE:
        lines.append(f"Source: {record.source}")
        if record.source_epoch:
            lines.append(f"Epoch: {record.source_epoch}")

    if tab not in {InspectorTab.USAGE, InspectorTab.TIMING, InspectorTab.SOURCE}:
        for link in record.links:
            lines.append(_participant_line(link))
            link_lines.append((len(lines) - 1, link.participant_id))
    return lines, link_lines


def inspector_text(record: TrajectoryRecord | None, tab: InspectorTab) -> str:
    """Build the exact bounded text exposed by the active inspector tab."""
    lines, _ = _inspector_lines(record, tab)
    text = "\n".join(plain_text(line) for line in lines)
    clipped, _, _ = clip_utf8(text, MAX_DETAIL_BYTES)
    return clipped


def inspector_link_line_ids(record: TrajectoryRecord | None, tab: InspectorTab) -> dict[int, str]:
    lines, link_lines = _inspector_lines(record, tab)
    safe_lines = [plain_text(line) for line in lines]
    text, _, _ = clip_utf8("\n".join(safe_lines), MAX_DETAIL_BYTES)
    visible_lines = text.splitlines()
    result: dict[int, str] = {}
    for entry_index, participant_id in link_lines:
        line_index = sum(line.count("\n") + 1 for line in safe_lines[:entry_index])
        expected = safe_lines[entry_index].splitlines()
        if not expected:
            continue
        end = line_index + len(expected)
        if end <= len(visible_lines) and visible_lines[line_index:end] == expected:
            result.update(dict.fromkeys(range(line_index, end), participant_id))
    return result


def inspector_content(record: TrajectoryRecord | None, tab: InspectorTab) -> Text:
    safe = sanitize_text(inspector_text(record, tab))
    safe, _, _ = clip_utf8(safe, MAX_DETAIL_BYTES)
    return Text(safe)


def tooltip_text(record: TrajectoryRecord) -> str:
    """Return a small bounded hover detail; keyboard inspection remains immediate."""
    text = (
        f"{record.kind.value} · {record.source}\n{record.summary}\n{format_duration(record.timing)}"
    )
    clipped, _, _ = clip_utf8(text, MAX_TOOLTIP_BYTES)
    return clipped


def count_label(value: str, count: int) -> str:
    return f"{value} ({count})"


def details_size(record: TrajectoryRecord) -> int:
    return sum(
        len(field.name.encode("utf-8")) + len(field.value.text.encode("utf-8"))
        for field in record.details
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
