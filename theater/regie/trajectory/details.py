"""Pure rendering for the full-height span detail panel."""

from __future__ import annotations

from dataclasses import dataclass

from rich.style import Style
from rich.text import Text

from theater.regie.trajectory.constants import TRAJECTORY_DETAIL_RECORD_MAX_BYTES
from theater.regie.trajectory.enums import InspectorTab
from theater.regie.trajectory.inspector import InspectorLine, tool_summary_lines
from theater.regie.trajectory.render import (
    detail_links_by_line,
    detail_record_links_by_line,
    detail_text,
    sanitize_text,
    tabs_for_record,
)
from theater.trajectory import (
    ContentFormat,
    DetailField,
    LinkDirection,
    ParticipantLink,
    TrajectoryRecord,
    TrajectoryRequest,
    bounded_preview,
)
from theater.trajectory.tools import TrajectoryToolOperation

DETAIL_PARTICIPANT_META = "trajectory_detail_participant"
DETAIL_PARTICIPANT_RELATION_META = "trajectory_detail_participant_relation"
DETAIL_PARTICIPANT_DIRECTION_META = "trajectory_detail_participant_direction"
DETAIL_PARTICIPANT_TARGET_META = "trajectory_detail_participant_target"
DETAIL_PARTICIPANT_CORRELATION_TYPE_META = "trajectory_detail_participant_correlation_type"
DETAIL_PARTICIPANT_CORRELATION_KEY_META = "trajectory_detail_participant_correlation_key"
DETAIL_PARTICIPANT_EXACT_META = "trajectory_detail_participant_exact"
DETAIL_PARTICIPANT_UNRESOLVED_META = "trajectory_detail_participant_unresolved"
DETAIL_RECORD_TARGET_META = "trajectory_detail_record_target"


@dataclass(frozen=True, slots=True)
class SpanDetails:
    tab: InspectorTab
    tabs: tuple[InspectorTab, ...]
    content: Text
    copy_text: str


def active_detail_tab(record: TrajectoryRecord, requested: InspectorTab) -> InspectorTab:
    tabs = tabs_for_record(record)
    return requested if requested in tabs else tabs[0]


def _participant_link_meta(link: ParticipantLink) -> dict[str, str]:
    meta = {
        DETAIL_PARTICIPANT_META: link.participant_id,
        DETAIL_PARTICIPANT_RELATION_META: link.relation,
        DETAIL_PARTICIPANT_DIRECTION_META: link.direction.value,
        DETAIL_PARTICIPANT_EXACT_META: "1" if link.target_record_id is not None else "0",
        DETAIL_PARTICIPANT_UNRESOLVED_META: "0",
    }
    if link.target_record_id is not None:
        meta[DETAIL_PARTICIPANT_TARGET_META] = link.target_record_id
    if link.correlation_type is not None:
        meta[DETAIL_PARTICIPANT_CORRELATION_TYPE_META] = link.correlation_type
        assert link.correlation_key is not None
        meta[DETAIL_PARTICIPANT_CORRELATION_KEY_META] = link.correlation_key
    return meta


def _styled_content(
    copy_text: str,
    *,
    accent_style: Style,
    participant_links: dict[int, ParticipantLink] | None = None,
    record_links: dict[int, str] | None = None,
) -> Text:
    participant_links = participant_links or {}
    record_links = record_links or {}
    content = Text(overflow="fold")
    for index, line in enumerate(copy_text.splitlines() or [""]):
        if index:
            content.append("\n")
        if link := participant_links.get(index):
            content.append(
                line,
                accent_style + Style(underline=True, meta=_participant_link_meta(link)),
            )
        elif record_id := record_links.get(index):
            content.append(
                line,
                accent_style + Style(underline=True, meta={DETAIL_RECORD_TARGET_META: record_id}),
            )
        elif index == 0:
            content.append(line, style="bold")
        elif line.startswith("No "):
            content.append(line, style="dim italic")
        elif ":" in line and not line.lstrip().startswith(("{", "[", '"')):
            key, value = line.split(":", 1)
            content.append(f"{key}:", style=accent_style + Style(bold=True))
            content.append(value)
        else:
            content.append(line)
    return content


def build_span_details(
    record: TrajectoryRecord,
    tab: InspectorTab,
    *,
    accent_style: Style | None = None,
    request: TrajectoryRequest | None = None,
) -> SpanDetails:
    tabs = tabs_for_record(record)
    active = active_detail_tab(record, tab)
    accent = accent_style or Style(dim=True)
    copy_text = detail_text(record, active, request)
    return SpanDetails(
        tab=active,
        tabs=tabs,
        content=_styled_content(
            copy_text,
            accent_style=accent,
            participant_links=detail_links_by_line(record, active, request),
            record_links=detail_record_links_by_line(record, active, request),
        ),
        copy_text=copy_text,
    )


_TOOL_TABS = (InspectorTab.SUMMARY, InspectorTab.INPUT, InspectorTab.RESULT, InspectorTab.TIMING)


def active_tool_detail_tab(_tool: TrajectoryToolOperation, requested: InspectorTab) -> InspectorTab:
    return requested if requested in _TOOL_TABS else InspectorTab.SUMMARY


def _tool_field_lines(fields: tuple[DetailField, ...], aliases: set[str]) -> list[str]:
    lines: list[str] = []
    for field in fields:
        name = field.name.casefold().replace("-", "_").replace(" ", "_")
        if name not in aliases:
            continue
        if field.format is ContentFormat.JSON:
            try:
                import json

                value = json.dumps(json.loads(field.preview.text), ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                value = field.preview.text
        else:
            value = field.preview.text
        values = sanitize_text(value).splitlines() or [""]
        lines.append(f"{field.name}: {values[0]}")
        lines.extend(values[1:])
        omission = f"… {field.preview.omitted_bytes} bytes omitted …"
        if field.preview.omitted_bytes and omission not in value:
            lines.append(f"… {field.preview.omitted_bytes} source bytes omitted …")
    return lines


def _tool_detail_lines(
    tool: TrajectoryToolOperation,
    tab: InspectorTab,
) -> tuple[InspectorLine, ...]:
    active = active_tool_detail_tab(tool, tab)
    if active is InspectorTab.SUMMARY:
        return tool_summary_lines(tool)
    if active is InspectorTab.INPUT:
        field_lines = _tool_field_lines(
            tool.call_details, {"args", "arguments", "input", "parameters", "tool_input"}
        )
        return tuple(InspectorLine(line) for line in (field_lines or ["No input supplied."]))
    if active is InspectorTab.RESULT:
        field_lines = _tool_field_lines(
            tool.result_details, {"output", "response", "result", "tool_result", "error"}
        )
        return tuple(InspectorLine(line) for line in (field_lines or ["No result supplied."]))
    timing = tool.timing
    if timing is None:
        return (InspectorLine("No timing supplied."),)
    timing_lines = tuple(
        InspectorLine(f"{name}: {value}")
        for name, value in timing.to_wire().items()
        if value is not None
    )
    return timing_lines or (InspectorLine("No timing supplied."),)


def _bounded_tool_details(
    tool: TrajectoryToolOperation,
    tab: InspectorTab,
) -> tuple[str, dict[int, str]]:
    lines = _tool_detail_lines(tool, tab)
    text = bounded_preview(
        "\n".join(sanitize_text(line.text) for line in lines),
        max_bytes=TRAJECTORY_DETAIL_RECORD_MAX_BYTES,
    ).text
    visible = text.splitlines()
    links = {
        index: line.target_record_id
        for index, line in enumerate(lines)
        if line.target_record_id is not None
        and index < len(visible)
        and visible[index] == sanitize_text(line.text)
    }
    return text, links


def tool_detail_text(tool: TrajectoryToolOperation, tab: InspectorTab) -> str:
    """Return copyable tool detail without fabricating a trajectory record."""
    return _bounded_tool_details(tool, tab)[0]


def build_tool_span_details(
    tool: TrajectoryToolOperation,
    tab: InspectorTab,
    *,
    accent_style: Style | None = None,
) -> SpanDetails:
    active = active_tool_detail_tab(tool, tab)
    accent = accent_style or Style(dim=True)
    copy_text, record_links = _bounded_tool_details(tool, active)
    return SpanDetails(
        tab=active,
        tabs=_TOOL_TABS,
        content=_styled_content(
            copy_text,
            accent_style=accent,
            record_links=record_links,
        ),
        copy_text=copy_text,
    )


def participant_link_from_meta(meta: dict[str, object]) -> ParticipantLink | None:
    participant_id = meta.get(DETAIL_PARTICIPANT_META)
    relation = meta.get(DETAIL_PARTICIPANT_RELATION_META)
    direction = meta.get(DETAIL_PARTICIPANT_DIRECTION_META)
    if (
        not isinstance(participant_id, str)
        or not isinstance(relation, str)
        or not isinstance(direction, str)
    ):
        return None
    target_record_id = meta.get(DETAIL_PARTICIPANT_TARGET_META)
    correlation_type = meta.get(DETAIL_PARTICIPANT_CORRELATION_TYPE_META)
    correlation_key = meta.get(DETAIL_PARTICIPANT_CORRELATION_KEY_META)
    if target_record_id is not None and not isinstance(target_record_id, str):
        return None
    if correlation_type is not None and not isinstance(correlation_type, str):
        return None
    if correlation_key is not None and not isinstance(correlation_key, str):
        return None
    try:
        return ParticipantLink(
            participant_id,
            relation,
            LinkDirection(direction),
            target_record_id=target_record_id,
            correlation_type=correlation_type,
            correlation_key=correlation_key,
        )
    except ValueError:
        return None


__all__ = [
    "DETAIL_PARTICIPANT_CORRELATION_KEY_META",
    "DETAIL_PARTICIPANT_CORRELATION_TYPE_META",
    "DETAIL_PARTICIPANT_DIRECTION_META",
    "DETAIL_PARTICIPANT_EXACT_META",
    "DETAIL_PARTICIPANT_META",
    "DETAIL_PARTICIPANT_RELATION_META",
    "DETAIL_PARTICIPANT_TARGET_META",
    "DETAIL_PARTICIPANT_UNRESOLVED_META",
    "DETAIL_RECORD_TARGET_META",
    "SpanDetails",
    "active_detail_tab",
    "active_tool_detail_tab",
    "build_span_details",
    "build_tool_span_details",
    "participant_link_from_meta",
    "tool_detail_text",
]
