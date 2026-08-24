"""Pure rendering for one inline trajectory detail row."""

from __future__ import annotations

from dataclasses import dataclass

from rich.style import Style
from rich.text import Text

from theater.regie.trajectory.enums import InspectorTab
from theater.regie.trajectory.render import (
    detail_link_line_ids,
    detail_text,
    tabs_for_record,
)
from theater.trajectory import TrajectoryRecord

DETAIL_PARTICIPANT_META = "trajectory_detail_participant"
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
) -> tuple[Text, str]:
    copy_text = detail_text(record, tab)
    links = detail_link_line_ids(record, tab)
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
        elif participant_id := links.get(index):
            content.append(
                line,
                accent_style
                + Style(underline=True, meta={DETAIL_PARTICIPANT_META: participant_id}),
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


def build_inline_details(
    record: TrajectoryRecord,
    tab: InspectorTab,
    *,
    max_height: int,
    accent_style: Style | None = None,
) -> InlineDetails:
    tabs = tabs_for_record(record)
    active = active_detail_tab(record, tab)
    accent = accent_style or Style(dim=True)
    height_limit = max(len(tabs), int(max_height), 1)
    content, copy_text = _content(record, active, height_limit, accent)
    height = min(height_limit, max(len(tabs), len(content.plain.splitlines()) or 1))
    return InlineDetails(
        tab=active,
        tabs=tabs,
        menu=_tab_menu(record, tabs, active, accent),
        content=content,
        copy_text=copy_text,
        height=height,
    )


__all__ = [
    "DETAIL_PARTICIPANT_META",
    "DETAIL_TAB_META",
    "InlineDetails",
    "active_detail_tab",
    "build_inline_details",
    "move_detail_tab",
]
