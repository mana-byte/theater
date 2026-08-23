"""Contextual bottom-drawer inspector for one bounded trajectory record."""

from __future__ import annotations

from math import isfinite

from rich.text import Text
from textual import events
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

from theater.regie.trajectory.constants import (
    INSPECTOR_MIN_HEIGHT,
    INSPECTOR_RESIZE_STEP,
    INSPECTOR_SCROLL_STEP,
    TRAJECTORY_INSPECTOR_RATIO_DEFAULT,
    TRAJECTORY_INSPECTOR_RATIO_MAX,
    TRAJECTORY_INSPECTOR_RATIO_MIN,
)
from theater.regie.trajectory.enums import InspectorTab
from theater.regie.trajectory.render import (
    inspector_content,
    inspector_link_line_ids,
    tabs_for_record,
)
from theater.trajectory import TrajectoryRecord


class InspectorTabChanged(Message):
    """The active contextual inspector tab changed."""

    def __init__(self, tab: InspectorTab) -> None:
        super().__init__()
        self.tab = tab


class InspectorParticipantLinkClicked(Message):
    """A rendered participant link was activated."""

    def __init__(self, participant_id: str) -> None:
        super().__init__()
        self.participant_id = participant_id


class InspectorResizeRequested(Message):
    """A deliberate keyboard or mouse gesture requested a drawer resize."""

    def __init__(self, delta: float) -> None:
        super().__init__()
        self.delta = delta


class Inspector(Static):
    """Render bounded tab content and keep ordinary wheel input for content."""

    can_focus = True

    DEFAULT_CSS = f"""
    Inspector {{
        width: 1fr;
        height: {TRAJECTORY_INSPECTOR_RATIO_DEFAULT * 100:.0f}%;
        min-height: {INSPECTOR_MIN_HEIGHT};
        border-top: solid $panel;
        padding: 0 1;
        overflow-y: auto;
    }}
    Inspector.-maximized {{
        height: 1fr;
    }}
    """

    def __init__(self, record: TrajectoryRecord | None = None, **kwargs) -> None:
        super().__init__("", markup=False, **kwargs)
        self._record = record
        self._tabs = tabs_for_record(record)
        self._tab = self._tabs[0]
        self._ratio = TRAJECTORY_INSPECTOR_RATIO_DEFAULT
        self._maximized = False
        self._link_line_ids: dict[int, str] = {}
        self._resizing = False
        self._render_content()

    @property
    def record(self) -> TrajectoryRecord | None:
        return self._record

    @property
    def tabs(self) -> tuple[InspectorTab, ...]:
        return self._tabs

    @property
    def tab(self) -> InspectorTab:
        return self._tab

    @property
    def ratio(self) -> float:
        return self._ratio

    @property
    def maximized(self) -> bool:
        return self._maximized

    @property
    def copy_text(self) -> str:
        return inspector_content(self._record, self._tab).plain

    def _render_content(self) -> None:
        labels = [f"[{tab.value}]" if tab == self._tab else tab.value for tab in self._tabs]
        heading = "  ".join(labels)
        content = inspector_content(self._record, self._tab)
        rendered = Text(heading, no_wrap=True, overflow="crop")
        rendered.append("\n")
        rendered.append_text(content)
        self._link_line_ids = {
            line_index + 1: participant_id
            for line_index, participant_id in inspector_link_line_ids(
                self._record, self._tab
            ).items()
        }
        self.update(rendered, layout=False)
        self.scroll_to(y=0, animate=False)

    def set_record(
        self, record: TrajectoryRecord | None, *, tab: InspectorTab | None = None
    ) -> None:
        self._record = record
        self._tabs = tabs_for_record(record)
        self._tab = tab if tab in self._tabs else self._tabs[0]
        self._render_content()

    def set_tab(self, tab: InspectorTab) -> InspectorTab:
        if tab not in self._tabs:
            return self._tab
        self._tab = tab
        self._render_content()
        self.post_message(InspectorTabChanged(tab))
        return tab

    def move_tab(self, delta: int) -> InspectorTab:
        if not self._tabs:
            return self._tab
        index = self._tabs.index(self._tab)
        index = max(0, min(len(self._tabs) - 1, index + delta))
        return self.set_tab(self._tabs[index])

    def set_ratio(self, ratio: float) -> float:
        if (
            not isinstance(ratio, (int, float))
            or isinstance(ratio, bool)
            or not isfinite(float(ratio))
        ):
            raise ValueError("inspector ratio must be finite")
        self._ratio = max(
            TRAJECTORY_INSPECTOR_RATIO_MIN,
            min(TRAJECTORY_INSPECTOR_RATIO_MAX, float(ratio)),
        )
        self._apply_height()
        return self._ratio

    def resize_by(self, delta: float) -> float:
        return self.set_ratio(self._ratio + delta)

    def toggle_maximize(self) -> bool:
        self._maximized = not self._maximized
        self.set_class(self._maximized, "-maximized")
        self._apply_height()
        return self._maximized

    def _apply_height(self) -> None:
        self.styles.height = "1fr" if self._maximized else f"{self._ratio * 100:.2f}%"

    def emit_participant_link(self, participant_id: str) -> None:
        if participant_id:
            self.post_message(InspectorParticipantLinkClicked(participant_id))

    def on_click(self, event: events.Click) -> None:
        participant_id = self._link_line_ids.get(int(event.y) + int(self.scroll_y))
        if participant_id is None:
            return
        event.stop()
        self.emit_participant_link(participant_id)

    def on_key(self, event: events.Key) -> None:
        if event.key in {"ctrl+left", "ctrl+up"}:
            event.stop()
            self.post_message(InspectorResizeRequested(-INSPECTOR_RESIZE_STEP))
        elif event.key in {"ctrl+right", "ctrl+down"}:
            event.stop()
            self.post_message(InspectorResizeRequested(INSPECTOR_RESIZE_STEP))

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button == 1 and int(event.y) == 0:
            event.stop()
            self._resizing = True
            self.capture_mouse()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._resizing:
            return
        event.stop()
        parent = self.parent
        height = parent.region.height if isinstance(parent, Widget) else self.region.height
        if height:
            self.post_message(InspectorResizeRequested(-event.delta_y / height))

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._resizing:
            event.stop()
            self._resizing = False
            self.release_mouse()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        event.stop()
        if event.shift:
            self.post_message(InspectorResizeRequested(-INSPECTOR_RESIZE_STEP))
        else:
            self.scroll_relative(y=-INSPECTOR_SCROLL_STEP, animate=False)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        event.stop()
        if event.shift:
            self.post_message(InspectorResizeRequested(INSPECTOR_RESIZE_STEP))
        else:
            self.scroll_relative(y=INSPECTOR_SCROLL_STEP, animate=False)


__all__ = [
    "Inspector",
    "InspectorParticipantLinkClicked",
    "InspectorResizeRequested",
    "InspectorTabChanged",
]
