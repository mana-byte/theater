"""Rich styled content for bounded trajectory inspection details."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from rich.style import Style
from rich.text import Text

from theater.regie.trajectory.enums import InspectorTab
from theater.regie.trajectory.inspection.links import (
    DETAIL_RECORD_TARGET_META,
    participant_link_meta,
)
from theater.regie.trajectory.inspection.project import (
    project_record_details,
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
    content: Text
    copy_text: str


def _styled_content(
    copy_text: str,
    *,
    accent_style: Style,
    participant_links: Mapping[int, ParticipantLink] | None = None,
    record_links: Mapping[int, str] | None = None,
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
                accent_style + Style(underline=True, meta=participant_link_meta(link)),
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
    projection = project_record_details(record, tab, request)
    accent = accent_style or Style(dim=True)
    return SpanDetails(
        tab=projection.tab,
        tabs=projection.tabs,
        content=_styled_content(
            projection.copy_text,
            accent_style=accent,
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
) -> SpanDetails:
    projection = project_tool_details(tool, tab)
    accent = accent_style or Style(dim=True)
    return SpanDetails(
        tab=projection.tab,
        tabs=projection.tabs,
        content=_styled_content(
            projection.copy_text,
            accent_style=accent,
            record_links=projection.record_links,
        ),
        copy_text=projection.copy_text,
    )


__all__ = [
    "SpanDetails",
    "build_span_details",
    "build_tool_span_details",
]
