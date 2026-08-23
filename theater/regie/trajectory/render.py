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
    InspectorTab,
    Lane,
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


def _detail_lines(record: TrajectoryRecord) -> list[str]:
    lines: list[str] = []
    for field in record.details:
        value = field.value.text
        if field.value.format in {ContentFormat.IMAGE, ContentFormat.BINARY}:
            value = f"{field.value.format.value} metadata ({field.value.original_bytes} bytes)"
        elif field.value.format is ContentFormat.JSON:
            try:
                value = json.dumps(json.loads(value), ensure_ascii=False, indent=2, sort_keys=True)
            except (TypeError, ValueError):
                value = plain_text(value)
        lines.append(f"{field.name}: {value}")
    lines.extend(
        f"participant {link.direction.value}: {link.participant_id} {link.label or ''}".rstrip()
        for link in record.links
    )
    return lines


def inspector_text(record: TrajectoryRecord | None, tab: InspectorTab) -> str:
    """Build the exact bounded text exposed by the active inspector tab."""
    if record is None:
        return "No record selected."
    lines = [
        f"{record.kind.value} · {record.source} · {status_label(record.status)}",
        record.summary,
    ]
    details = _detail_lines(record)
    if tab in {InspectorTab.SUMMARY, InspectorTab.PREVIEW, InspectorTab.CURRENT}:
        lines.extend(details[:8])
    elif tab in {InspectorTab.OUTPUT, InspectorTab.RESULT, InspectorTab.PAYLOAD, InspectorTab.RAW}:
        lines.extend(details)
    elif tab == InspectorTab.INPUT:
        lines.extend(
            line for line in details if "input" in line.casefold() or "argument" in line.casefold()
        )
    elif tab == InspectorTab.REASONING:
        lines.extend(line for line in details if "reason" in line.casefold())
    elif tab == InspectorTab.USAGE:
        lines.append(
            json.dumps(
                record.usage.to_wire() if record.usage else "No usage recorded.",
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    elif tab == InspectorTab.TIMING:
        lines.append(f"Duration: {format_duration(record.timing)}")
        if record.timing:
            lines.extend(
                value
                for value in (record.timing.started_at, record.timing.finished_at)
                if value is not None
            )
    elif tab == InspectorTab.SOURCE:
        lines.append(f"Source: {record.source}")
        if record.source_epoch:
            lines.append(f"Epoch: {record.source_epoch}")
    elif tab == InspectorTab.PREVIOUS:
        lines.append("No previous context supplied.")
    elif tab == InspectorTab.DIFF:
        lines.append("No context diff supplied.")
    text = "\n".join(plain_text(line) for line in lines)
    clipped, _, _ = clip_utf8(text, MAX_DETAIL_BYTES)
    return clipped


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
