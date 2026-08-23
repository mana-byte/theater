"""Selectable lane, kind, status, and source filter chooser."""

from __future__ import annotations

from rich.text import Text
from textual import events
from textual.message import Message
from textual.widgets import Static

from theater.regie.trajectory.constants import FILTER_MAX_ROWS
from theater.regie.trajectory.models import (
    FilterDimension,
    Lane,
    RecordKind,
    RecordStatus,
)
from theater.regie.trajectory.render import sanitize_text
from theater.regie.trajectory.search import FilterCounts


class FilterValueClicked(Message):
    """A chooser value was activated."""

    def __init__(self, dimension: FilterDimension, value: str) -> None:
        super().__init__()
        self.dimension = dimension
        self.value = value


class FilterPanelClosed(Message):
    """The chooser requested focus return to the trajectory region."""


class FilterPanel(Static):
    """Show every available filter value with its current structural count."""

    can_focus = True

    DEFAULT_CSS = f"""
    FilterPanel {{
        width: 1fr;
        height: auto;
        max-height: {FILTER_MAX_ROWS};
        padding: 0 1;
        overflow-y: auto;
        background: $panel;
    }}
    """

    def __init__(self, **kwargs) -> None:
        super().__init__("", markup=False, **kwargs)
        self._options: list[tuple[FilterDimension, str]] = []
        self._cursor = 0

    @property
    def options(self) -> tuple[tuple[FilterDimension, str], ...]:
        return tuple(self._options)

    def _append_dimension(
        self,
        dimension: FilterDimension,
        values: list[tuple[str, int]],
        selected: set[str],
        lines: list[str],
    ) -> None:
        for value, count in values:
            self._options.append((dimension, value))
            marker = "✓" if value in selected else " "
            label = value.replace("_", " ")
            lines.append(f"{marker} {dimension.value:<6} {sanitize_text(label)} ({count})")

    def update_filters(
        self,
        counts: FilterCounts,
        *,
        lanes: set[Lane],
        kinds: set[RecordKind],
        statuses: set[RecordStatus],
        sources: set[str],
    ) -> None:
        self._options = []
        lines: list[str] = []
        self._append_dimension(
            FilterDimension.LANE,
            [(lane.value, counts.lanes.get(lane, 0)) for lane in Lane],
            {lane.value for lane in lanes},
            lines,
        )
        self._append_dimension(
            FilterDimension.KIND,
            [(kind.value, counts.kinds.get(kind, 0)) for kind in RecordKind],
            {kind.value for kind in kinds},
            lines,
        )
        self._append_dimension(
            FilterDimension.STATUS,
            [(status.value, counts.statuses.get(status, 0)) for status in RecordStatus],
            {status.value for status in statuses},
            lines,
        )
        source_values = sorted(set(counts.sources) | sources)
        self._append_dimension(
            FilterDimension.SOURCE,
            [(source, counts.sources.get(source, 0)) for source in source_values],
            sources,
            lines,
        )
        self._cursor = min(self._cursor, max(0, len(self._options) - 1))
        content = Text("\n".join(lines) or "No filter values in the loaded window.", no_wrap=True)
        self.update(content, layout=False)

    def _activate_cursor(self) -> None:
        if not self._options:
            return
        dimension, value = self._options[self._cursor]
        self.post_message(FilterValueClicked(dimension, value))

    def on_key(self, event: events.Key) -> None:
        if event.key in {"up", "k"}:
            event.stop()
            self._cursor = max(0, self._cursor - 1)
        elif event.key in {"down", "j"}:
            event.stop()
            self._cursor = min(max(0, len(self._options) - 1), self._cursor + 1)
        elif event.key in {"enter", "space"}:
            event.stop()
            self._activate_cursor()
        elif event.key == "escape":
            event.stop()
            self.post_message(FilterPanelClosed())

    def on_click(self, event: events.Click) -> None:
        index = int(event.y) + int(self.scroll_y)
        if index < 0 or index >= len(self._options):
            return
        event.stop()
        self._cursor = index
        self._activate_cursor()


__all__ = ["FilterPanel", "FilterPanelClosed", "FilterValueClicked"]
