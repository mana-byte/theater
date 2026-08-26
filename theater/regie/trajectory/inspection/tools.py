"""Plain bounded detail projection for trajectory tool operations."""

from __future__ import annotations

import json
from dataclasses import dataclass

from theater.constants.trajectory import TRAJECTORY_DETAIL_RECORD_MAX_BYTES
from theater.regie.trajectory.enums import InspectorTab
from theater.regie.trajectory.inspection.lines import InspectorLine, tool_summary_lines
from theater.regie.trajectory.render.formatting import sanitize_text
from theater.trajectory import ContentFormat, DetailField, bounded_preview
from theater.trajectory.tools import TrajectoryToolOperation

_TOOL_TABS = (InspectorTab.SUMMARY, InspectorTab.INPUT, InspectorTab.RESULT, InspectorTab.TIMING)


@dataclass(frozen=True, slots=True)
class ToolDetailProjection:
    tab: InspectorTab
    tabs: tuple[InspectorTab, ...]
    copy_text: str
    record_links: dict[int, str]


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


def project_tool_details(
    tool: TrajectoryToolOperation,
    tab: InspectorTab,
) -> ToolDetailProjection:
    active = active_tool_detail_tab(tool, tab)
    lines = _tool_detail_lines(tool, active)
    text = bounded_preview(
        "\n".join(sanitize_text(line.text) for line in lines),
        max_bytes=TRAJECTORY_DETAIL_RECORD_MAX_BYTES,
    ).text
    visible = text.splitlines()
    record_links = {
        index: line.target_record_id
        for index, line in enumerate(lines)
        if line.target_record_id is not None
        and index < len(visible)
        and visible[index] == sanitize_text(line.text)
    }
    return ToolDetailProjection(active, _TOOL_TABS, text, record_links)


def tool_detail_text(tool: TrajectoryToolOperation, tab: InspectorTab) -> str:
    """Return copyable tool detail without fabricating a trajectory record."""
    return project_tool_details(tool, tab).copy_text


__all__ = [
    "ToolDetailProjection",
    "active_tool_detail_tab",
    "project_tool_details",
    "tool_detail_text",
]
