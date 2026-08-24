"""Pure rendering for one inline trajectory detail row."""

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
DETAIL_TAB_META = "trajectory_detail_tab"


@dataclass(frozen=True, slots=True)
class InlineDetails:
    tab: InspectorTab
    tabs: tuple[InspectorTab, ...]
    menu: Text
    content: Text
    copy_text: str
    height: int


def active_detail_tab(record: TrajectoryRecord, requested: InspectorTab) -> InspectorTab:
    tabs = tabs_for_record(record)
    return requested if requested in tabs else tabs[0]


def move_detail_tab(record: TrajectoryRecord, active: InspectorTab, delta: int) -> InspectorTab:
    tabs = tabs_for_record(record)
    current = tabs.index(active) if active in tabs else 0
    return tabs[max(0, min(len(tabs) - 1, current + delta))]


def _tab_menu(
    record: TrajectoryRecord,
    tabs: tuple[InspectorTab, ...],
    active: InspectorTab,
    accent_style: Style,
) -> Text:
    menu = Text(no_wrap=True, overflow="ellipsis")
    for index, tab in enumerate(tabs):
        if index:
            menu.append("\n")
        selected = tab is active
        state_style = Style(
            bold=selected,
            dim=not selected,
            meta={DETAIL_TAB_META: tab.value, "trajectory_detail_record": record.record_id},
        )
        style = accent_style + state_style if selected else state_style
        menu.append(f"{'▸' if selected else ' '} {tab.value.replace('_', ' ').upper()}", style)
    return menu


def _content(
    record: TrajectoryRecord,
    tab: InspectorTab,
    max_height: int,
    accent_style: Style,
    request: TrajectoryRequest | None,
) -> tuple[Text, str]:
    copy_text = detail_text(record, tab, request)
    links = detail_links_by_line(record, tab, request)
    record_links = detail_record_links_by_line(record, tab, request)
    lines = copy_text.splitlines() or [""]
    clipped = len(lines) > max_height
    visible = lines[:max_height]
    if clipped:
        visible[-1] = "… preview clipped"
    content = Text(no_wrap=True, overflow="ellipsis")
    for index, line in enumerate(visible):
        if index:
            content.append("\n")
        if clipped and index == len(visible) - 1:
            content.append(line, style="dim italic")
        elif link := links.get(index):
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
    return content, copy_text


def _participant_link_meta(link) -> dict[str, str]:
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


def build_inline_details(
    record: TrajectoryRecord,
    tab: InspectorTab,
    *,
    max_height: int,
    accent_style: Style | None = None,
    request: TrajectoryRequest | None = None,
) -> InlineDetails:
    tabs = tabs_for_record(record)
    active = active_detail_tab(record, tab)
    accent = accent_style or Style(dim=True)
    height_limit = max(len(tabs), int(max_height), 1)
    content, copy_text = _content(record, active, height_limit, accent, request)
    height = min(height_limit, max(len(tabs), len(content.plain.splitlines()) or 1))
    return InlineDetails(
        tab=active,
        tabs=tabs,
        menu=_tab_menu(record, tabs, active, accent),
        content=content,
        copy_text=copy_text,
        height=height,
    )


_TOOL_TABS = (InspectorTab.SUMMARY, InspectorTab.INPUT, InspectorTab.RESULT, InspectorTab.TIMING)


def active_tool_detail_tab(_tool: TrajectoryToolOperation, requested: InspectorTab) -> InspectorTab:
    return requested if requested in _TOOL_TABS else InspectorTab.SUMMARY


def move_tool_detail_tab(
    _tool: TrajectoryToolOperation, active: InspectorTab, delta: int
) -> InspectorTab:
    current = _TOOL_TABS.index(active) if active in _TOOL_TABS else 0
    return _TOOL_TABS[max(0, min(len(_TOOL_TABS) - 1, current + delta))]


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


def build_tool_inline_details(
    tool: TrajectoryToolOperation,
    tab: InspectorTab,
    *,
    max_height: int,
    accent_style: Style | None = None,
) -> InlineDetails:
    active = active_tool_detail_tab(tool, tab)
    accent = accent_style or Style(dim=True)
    menu = Text(no_wrap=True, overflow="ellipsis")
    for index, candidate in enumerate(_TOOL_TABS):
        if index:
            menu.append("\n")
        selected = candidate is active
        menu.append(
            f"{'▸' if selected else ' '} {candidate.value.upper()}",
            accent
            + Style(bold=selected, dim=not selected, meta={DETAIL_TAB_META: candidate.value}),
        )
    copy_text, record_links = _bounded_tool_details(tool, active)
    lines = copy_text.splitlines() or [""]
    limit = max(len(_TOOL_TABS), int(max_height), 1)
    visible = lines[:limit]
    if len(lines) > limit:
        visible[-1] = "… preview clipped"
    content = Text(no_wrap=True, overflow="ellipsis")
    for index, line in enumerate(visible):
        if index:
            content.append("\n")
        if record_id := record_links.get(index):
            content.append(
                line,
                accent + Style(underline=True, meta={DETAIL_RECORD_TARGET_META: record_id}),
            )
        else:
            content.append(line)
    return InlineDetails(
        tab=active,
        tabs=_TOOL_TABS,
        menu=menu,
        content=content,
        copy_text=copy_text,
        height=min(limit, max(len(_TOOL_TABS), len(visible))),
    )


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
    "DETAIL_TAB_META",
    "InlineDetails",
    "active_detail_tab",
    "active_tool_detail_tab",
    "build_inline_details",
    "build_tool_inline_details",
    "move_detail_tab",
    "move_tool_detail_tab",
    "tool_detail_text",
]
