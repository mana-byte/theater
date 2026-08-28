"""Plain bounded detail projection for trajectory tool operations."""

from __future__ import annotations

import json
from dataclasses import dataclass

from theater.constants.trajectory import TRAJECTORY_DETAIL_RECORD_MAX_BYTES
from theater.regie.trajectory.enums import InspectorTab
from theater.regie.trajectory.inspection.blocks import DetailBlock
from theater.regie.trajectory.inspection.lines import InspectorLine, tool_summary_lines
from theater.regie.trajectory.render.formatting import sanitize_text
from theater.trajectory import ContentFormat, DetailField, bounded_preview
from theater.trajectory.tools import TrajectoryToolOperation

_INPUT_ALIASES = frozenset({"args", "arguments", "input", "parameters", "tool_input"})
_RESULT_ALIASES = frozenset({"output", "response", "result", "tool_result", "error"})


@dataclass(frozen=True, slots=True)
class ToolDetailProjection:
    tab: InspectorTab
    tabs: tuple[InspectorTab, ...]
    copy_text: str
    record_links: dict[int, str]
    blocks: tuple[DetailBlock, ...]


def _has_tool_fields(fields: tuple[DetailField, ...], aliases: frozenset[str]) -> bool:
    return any(
        field.name.casefold().replace("-", "_").replace(" ", "_") in aliases for field in fields
    )


def tabs_for_tool(tool: TrajectoryToolOperation) -> tuple[InspectorTab, ...]:
    tabs = [InspectorTab.SUMMARY]
    if _has_tool_fields(tool.call_details, _INPUT_ALIASES):
        tabs.append(InspectorTab.INPUT)
    if _has_tool_fields(tool.result_details, _RESULT_ALIASES):
        tabs.append(InspectorTab.RESULT)
    if tool.timing is not None:
        tabs.append(InspectorTab.TIMING)
    return tuple(tabs)


def active_tool_detail_tab(tool: TrajectoryToolOperation, requested: InspectorTab) -> InspectorTab:
    tabs = tabs_for_tool(tool)
    return requested if requested in tabs else tabs[0]


def _tool_field_lines(
    fields: tuple[DetailField, ...], aliases: frozenset[str]
) -> tuple[list[str], list[DetailBlock]]:
    lines: list[str] = []
    blocks: list[DetailBlock] = []
    for field in fields:
        name = field.name.casefold().replace("-", "_").replace(" ", "_")
        if name not in aliases:
            continue
        if field.format is ContentFormat.JSON:
            try:
                value = json.dumps(json.loads(field.preview.text), ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                value = field.preview.text
        else:
            value = field.preview.text
        values = sanitize_text(value).splitlines() or [""]
        start_line = len(lines)
        lines.append(f"{field.name}: {values[0]}")
        lines.extend(values[1:])
        blocks.append(DetailBlock(start_line, len(lines), field.format, field.name))
        omission = f"… {field.preview.omitted_bytes} bytes omitted …"
        if field.preview.omitted_bytes and omission not in value:
            lines.append(f"… {field.preview.omitted_bytes} source bytes omitted …")
    return lines, blocks


def _tool_detail_lines(
    tool: TrajectoryToolOperation,
    tab: InspectorTab,
) -> tuple[tuple[InspectorLine, ...], tuple[DetailBlock, ...]]:
    if tab is InspectorTab.SUMMARY:
        return tool_summary_lines(tool), ()
    if tab is InspectorTab.INPUT:
        field_lines, blocks = _tool_field_lines(tool.call_details, _INPUT_ALIASES)
        return (
            tuple(InspectorLine(line) for line in (field_lines or ["No input supplied."])),
            tuple(blocks),
        )
    if tab is InspectorTab.RESULT:
        field_lines, blocks = _tool_field_lines(tool.result_details, _RESULT_ALIASES)
        return (
            tuple(InspectorLine(line) for line in (field_lines or ["No result supplied."])),
            tuple(blocks),
        )
    if tab is not InspectorTab.TIMING:
        return tool_summary_lines(tool), ()
    timing = tool.timing
    if timing is None:
        return (InspectorLine("No timing supplied."),), ()
    timing_lines = tuple(
        InspectorLine(f"{name}: {value}")
        for name, value in timing.to_wire().items()
        if value is not None
    )
    return timing_lines or (InspectorLine("No timing supplied."),), ()


def _project_tool_details(
    tool: TrajectoryToolOperation,
    tab: InspectorTab,
    *,
    resolve_tab: bool,
) -> ToolDetailProjection:
    tabs = tabs_for_tool(tool)
    active = tab if not resolve_tab or tab in tabs else tabs[0]
    lines, blocks = _tool_detail_lines(tool, active)
    text = bounded_preview(
        "\n".join(sanitize_text(line.text) for line in lines),
        max_bytes=TRAJECTORY_DETAIL_RECORD_MAX_BYTES,
    ).text
    visible = text.splitlines()
    visible_blocks = tuple(
        clipped for block in blocks if (clipped := block.clipped(len(visible))) is not None
    )
    record_links = {
        index: line.target_record_id
        for index, line in enumerate(lines)
        if line.target_record_id is not None
        and index < len(visible)
        and visible[index] == sanitize_text(line.text)
    }
    return ToolDetailProjection(active, tabs, text, record_links, visible_blocks)


def project_tool_details(
    tool: TrajectoryToolOperation,
    tab: InspectorTab,
) -> ToolDetailProjection:
    return _project_tool_details(tool, tab, resolve_tab=True)


def tool_detail_text(tool: TrajectoryToolOperation, tab: InspectorTab) -> str:
    """Return copyable tool detail without fabricating a trajectory record."""
    return _project_tool_details(tool, tab, resolve_tab=False).copy_text


__all__ = [
    "ToolDetailProjection",
    "active_tool_detail_tab",
    "project_tool_details",
    "tabs_for_tool",
    "tool_detail_text",
]
