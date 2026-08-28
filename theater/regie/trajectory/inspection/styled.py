"""Rich styled content for bounded trajectory inspection details."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from rich.console import RenderableType
from rich.style import Style
from rich.text import Text

from theater.regie.trajectory.enums import InspectorTab
from theater.regie.trajectory.inspection.blocks import DetailBlock
from theater.regie.trajectory.inspection.links import (
    DETAIL_RECORD_TARGET_META,
    participant_link_meta,
)
from theater.regie.trajectory.inspection.project import (
    project_record_details,
)
from theater.regie.trajectory.inspection.rich_content import (
    DetailDocument,
    DetailStyles,
    formatted_value,
)
from theater.regie.trajectory.inspection.tools import project_tool_details
from theater.trajectory import (
    ParticipantLink,
    TrajectoryRecord,
    TrajectoryRequest,
)
from theater.trajectory.tools import TrajectoryToolOperation


@dataclass(frozen=True, slots=True)
class SpanDetails:
    tab: InspectorTab
    tabs: tuple[InspectorTab, ...]
    content: DetailDocument
    copy_text: str


def _styled_lines(
    lines: list[str],
    start_line: int,
    *,
    styles: DetailStyles,
    participant_links: Mapping[int, ParticipantLink] | None = None,
    record_links: Mapping[int, str] | None = None,
) -> Text:
    participant_links = participant_links or {}
    record_links = record_links or {}
    content = Text(style=styles.text, overflow="fold")
    for offset, line in enumerate(lines):
        index = start_line + offset
        if offset:
            content.append("\n")
        if link := participant_links.get(index):
            content.append(
                line,
                styles.accent + Style(underline=True, meta=participant_link_meta(link)),
            )
        elif record_id := record_links.get(index):
            content.append(
                line,
                styles.accent + Style(underline=True, meta={DETAIL_RECORD_TARGET_META: record_id}),
            )
        elif index == 0:
            content.append(line, style=styles.text + Style(bold=True))
        elif line.startswith("No "):
            content.append(line, style=styles.muted + Style(italic=True))
        elif ":" in line and not line.lstrip().startswith(("{", "[", '"')):
            key, value = line.split(":", 1)
            content.append(f"{key}:", style=styles.accent + Style(bold=True))
            content.append(value)
        else:
            content.append(line)
    return content


def _block_value(lines: list[str], block: DetailBlock) -> str:
    values = lines[block.start_line : block.end_line]
    if not values:
        return ""
    if block.label is not None:
        prefix = f"{block.label}:"
        if values[0].startswith(prefix):
            values[0] = values[0][len(prefix) :].removeprefix(" ")
    return "\n".join(values)


def _styled_content(
    copy_text: str,
    *,
    styles: DetailStyles,
    blocks: tuple[DetailBlock, ...] = (),
    collapsed_json_paths: frozenset[str] = frozenset(),
    participant_links: Mapping[int, ParticipantLink] | None = None,
    record_links: Mapping[int, str] | None = None,
) -> DetailDocument:
    lines = copy_text.splitlines() or [""]
    renderables: list[RenderableType] = []
    cursor = 0
    for block_index, block in enumerate(blocks):
        if block.start_line > cursor:
            renderables.append(
                _styled_lines(
                    lines[cursor : block.start_line],
                    cursor,
                    styles=styles,
                    participant_links=participant_links,
                    record_links=record_links,
                )
            )
        if block.label is not None:
            label = block.label.replace("_", " ").replace("-", " ").title()
            renderables.append(Text(label, style=styles.accent + Style(bold=True)))
        renderables.append(
            formatted_value(
                _block_value(lines, block),
                block.format,
                styles,
                scope=f"{block_index}:{block.label or 'content'}",
                collapsed_json_paths=collapsed_json_paths,
            )
        )
        cursor = block.end_line
    if cursor < len(lines):
        renderables.append(
            _styled_lines(
                lines[cursor:],
                cursor,
                styles=styles,
                participant_links=participant_links,
                record_links=record_links,
            )
        )
    return DetailDocument(tuple(renderables), copy_text)


def build_span_details(
    record: TrajectoryRecord,
    tab: InspectorTab,
    *,
    accent_style: Style | None = None,
    styles: DetailStyles | None = None,
    request: TrajectoryRequest | None = None,
    collapsed_json_paths: frozenset[str] = frozenset(),
) -> SpanDetails:
    projection = project_record_details(record, tab, request)
    accent = accent_style or Style(dim=True)
    detail_styles = styles or DetailStyles.fallback(accent)
    return SpanDetails(
        tab=projection.tab,
        tabs=projection.tabs,
        content=_styled_content(
            projection.copy_text,
            styles=detail_styles,
            blocks=projection.blocks,
            collapsed_json_paths=collapsed_json_paths,
            participant_links=projection.participant_links,
            record_links=projection.record_links,
        ),
        copy_text=projection.copy_text,
    )


def build_tool_span_details(
    tool: TrajectoryToolOperation,
    tab: InspectorTab,
    *,
    accent_style: Style | None = None,
    styles: DetailStyles | None = None,
    collapsed_json_paths: frozenset[str] = frozenset(),
) -> SpanDetails:
    projection = project_tool_details(tool, tab)
    accent = accent_style or Style(dim=True)
    detail_styles = styles or DetailStyles.fallback(accent)
    return SpanDetails(
        tab=projection.tab,
        tabs=projection.tabs,
        content=_styled_content(
            projection.copy_text,
            styles=detail_styles,
            blocks=projection.blocks,
            collapsed_json_paths=collapsed_json_paths,
            record_links=projection.record_links,
        ),
        copy_text=projection.copy_text,
    )


__all__ = [
    "SpanDetails",
    "build_span_details",
    "build_tool_span_details",
]
