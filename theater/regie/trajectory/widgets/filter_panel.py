"""Native selectable filters for the loaded trajectory window."""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Label, SelectionList

from theater.constants.regie_trajectory import (
    FILTER_HEADER_HEIGHT,
    FILTER_MAX_ROWS,
    SEARCH_HEIGHT,
)
from theater.regie.trajectory.enums import FilterDimension
from theater.regie.trajectory.render.records import sanitize_text
from theater.regie.trajectory.search import FilterCounts
from theater.trajectory import TrajectoryKind, TrajectoryLane, TrajectoryStatus

FilterValue = tuple[FilterDimension, str]


class FilterValueClicked(Message):
    """A chooser value was activated."""

    def __init__(self, dimension: FilterDimension, value: str) -> None:
        super().__init__()
        self.dimension = dimension
        self.value = value


class FilterPanelClosed(Message):
    """The chooser requested focus return to trajectory."""


class FilterClearRequested(Message):
    """The chooser requested clearing every active filter."""


class FilterPanel(Vertical):
    """Show typed filter options through Textual's native selection list."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "trajectory-filter--active",
        "trajectory-filter--dimension",
    }

    DEFAULT_CSS = f"""
    FilterPanel {{
        width: 1fr;
        height: {FILTER_MAX_ROWS};
        max-height: {FILTER_MAX_ROWS};
        dock: top;
        layer: trajectory-overlay;
        offset-y: {SEARCH_HEIGHT};
        background: $background;
        border: solid $accent 20%;
        padding: 0 1;
    }}
    FilterPanel > #trajectory-filter-header {{
        width: 1fr;
        height: {FILTER_HEADER_HEIGHT};
        align-vertical: middle;
    }}
    FilterPanel #trajectory-filter-title {{
        width: auto;
        min-width: 10;
        height: 3;
        content-align: left middle;
        text-style: bold;
        color: $text;
    }}
    FilterPanel #trajectory-filter-summary {{
        width: 1fr;
        height: 3;
        content-align: left middle;
        color: $text-muted;
        padding: 0 1;
    }}
    FilterPanel Button {{
        min-width: 8;
        height: 3;
        content-align: center middle;
        border: none !important;
        margin: 0 0 0 1;
        color: $text-muted;
        background: $foreground 3%;
    }}
    FilterPanel Button:hover,
    FilterPanel Button:focus {{
        color: $text;
        background: $accent 10%;
    }}
    FilterPanel SelectionList {{
        width: 1fr;
        height: 1fr;
        border: none;
        padding: 0;
        background: $background;
    }}
    FilterPanel SelectionList > .option-list--option {{
        padding: 1 1;
    }}
    FilterPanel SelectionList > .option-list--option-highlighted {{
        background: $accent 20%;
        color: $text;
        text-style: bold;
    }}
    FilterPanel SelectionList > .option-list--option-hover {{
        background: $accent 10%;
    }}
    FilterPanel > .trajectory-filter--dimension {{
        color: $text-muted;
        text-style: bold;
    }}
    FilterPanel > .trajectory-filter--active {{
        color: $accent;
        text-style: dim;
    }}
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._options: list[FilterValue] = []
        self._cursor = 0
        self._scroll_offset = 0
        self._updating = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="trajectory-filter-header"):
            yield Label("FILTERS", id="trajectory-filter-title")
            yield Label("No active filters", id="trajectory-filter-summary")
            yield Button("Clear", id="trajectory-filter-clear", compact=True, flat=True)
            yield Button("Done", id="trajectory-filter-done", compact=True, flat=True)
        yield SelectionList[FilterValue](id="trajectory-filter-options", compact=True)

    @property
    def options(self) -> tuple[FilterValue, ...]:
        return tuple(self._options)

    def _prompt(self, dimension: FilterDimension, value: str, count: int, selected: bool) -> Text:
        prompt = Text(no_wrap=True, overflow="ellipsis")
        prompt.append(
            f"{dimension.value.upper():<7}",
            style=self.get_component_rich_style(
                "trajectory-filter--dimension",
                partial=True,
            ),
        )
        prompt.append(f" {sanitize_text(value.replace('_', ' '))}")
        prompt.append(f"  {count:>4}", style="dim")
        if selected:
            prompt.append(
                "  active",
                style=self.get_component_rich_style(
                    "trajectory-filter--active",
                    partial=True,
                ),
            )
        return prompt

    def _append_dimension(
        self,
        dimension: FilterDimension,
        values: list[tuple[str, int]],
        selected: set[str],
        choices: list[tuple[Text, FilterValue, bool]],
    ) -> None:
        for value, count in values:
            option = (dimension, value)
            self._options.append(option)
            choices.append(
                (
                    self._prompt(dimension, value, count, value in selected),
                    option,
                    value in selected,
                )
            )

    def update_filters(
        self,
        counts: FilterCounts,
        *,
        lanes: set[TrajectoryLane],
        kinds: set[TrajectoryKind],
        statuses: set[TrajectoryStatus],
        sources: set[str],
    ) -> None:
        highlighted: FilterValue | None = None
        scroll_y = 0
        selection_list = (
            self.query_one("#trajectory-filter-options", SelectionList) if self.is_mounted else None
        )
        if selection_list is not None:
            scroll_y = int(selection_list.scroll_y)
            if selection_list.highlighted is not None and self._options:
                index = min(selection_list.highlighted, len(self._options) - 1)
                highlighted = self._options[index]
        self._options = []
        choices: list[tuple[Text, FilterValue, bool]] = []
        self._append_dimension(
            FilterDimension.LANE,
            [(lane.value, counts.lanes.get(lane, 0)) for lane in TrajectoryLane],
            {lane.value for lane in lanes},
            choices,
        )
        self._append_dimension(
            FilterDimension.KIND,
            [
                (kind.value, counts.kinds.get(kind, 0))
                for kind in TrajectoryKind
                if kind is not TrajectoryKind.USAGE
            ],
            {kind.value for kind in kinds},
            choices,
        )
        self._append_dimension(
            FilterDimension.STATUS,
            [(status.value, counts.statuses.get(status, 0)) for status in TrajectoryStatus],
            {status.value for status in statuses},
            choices,
        )
        source_values = sorted(set(counts.sources) | sources)
        self._append_dimension(
            FilterDimension.SOURCE,
            [(source, counts.sources.get(source, 0)) for source in source_values],
            sources,
            choices,
        )
        active_count = len(lanes) + len(kinds) + len(statuses) + len(sources)
        if not self.is_mounted:
            return
        self._updating = True
        try:
            selection_list = self.query_one("#trajectory-filter-options", SelectionList)
            selection_list.clear_options()
            selection_list.add_options(choices)
            if highlighted in self._options:
                selection_list.highlighted = self._options.index(highlighted)
            elif self._options:
                selection_list.highlighted = min(self._cursor, len(self._options) - 1)
            self._cursor = selection_list.highlighted or 0
            selection_list.scroll_y = scroll_y
            self._scroll_offset = int(selection_list.scroll_y)
        finally:
            self._updating = False
        summary = "No active filters" if not active_count else f"{active_count} active"
        self.query_one("#trajectory-filter-summary", Label).update(summary)

    def focus_options(self) -> None:
        if self.is_mounted:
            self.query_one("#trajectory-filter-options", SelectionList).focus()

    def _sync_scroll_state(self) -> None:
        if not self.is_mounted:
            return
        selection_list = self.query_one("#trajectory-filter-options", SelectionList)
        self._scroll_offset = int(selection_list.scroll_y)

    def on_selection_list_selection_highlighted(
        self, message: SelectionList.SelectionHighlighted[FilterValue]
    ) -> None:
        if message.selection_list.id != "trajectory-filter-options":
            return
        self._cursor = message.selection_index
        self.call_after_refresh(self._sync_scroll_state)

    def on_selection_list_selection_toggled(
        self, message: SelectionList.SelectionToggled[FilterValue]
    ) -> None:
        if self._updating or message.selection_list.id != "trajectory-filter-options":
            return
        dimension, value = message.selection.value
        self.post_message(FilterValueClicked(dimension, value))
        message.stop()

    def on_button_pressed(self, message: Button.Pressed) -> None:
        if message.button.id == "trajectory-filter-clear":
            self.post_message(FilterClearRequested())
        elif message.button.id == "trajectory-filter-done":
            self.post_message(FilterPanelClosed())
        else:
            return
        message.stop()

    def on_key(self, event: events.Key) -> None:
        selection_list = self.query_one("#trajectory-filter-options", SelectionList)
        if event.key == "j":
            event.stop()
            selection_list.action_cursor_down()
        elif event.key == "k":
            event.stop()
            selection_list.action_cursor_up()
        elif event.key == "escape":
            event.stop()
            self.post_message(FilterPanelClosed())


__all__ = [
    "FilterClearRequested",
    "FilterPanel",
    "FilterPanelClosed",
    "FilterValueClicked",
]
