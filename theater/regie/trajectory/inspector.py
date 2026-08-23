"""Contextual bottom-drawer inspector for one bounded trajectory record."""

from __future__ import annotations

from textual import events
from textual.message import Message
from textual.widgets import Static

from theater.regie.trajectory.models import (
    MAX_INSPECTOR_RATIO,
    MIN_INSPECTOR_RATIO,
    InspectorTab,
    TrajectoryRecord,
)
from theater.regie.trajectory.render import inspector_content, tabs_for_record


class InspectorTabChanged(Message):
    """The active contextual inspector tab changed."""

    def __init__(self, tab: InspectorTab) -> None:
        super().__init__()
        self.tab = tab


class InspectorParticipantLinkClicked(Message):
    """A later app integration can stage this participant without knowing widgets."""

    def __init__(self, participant_id: str) -> None:
        super().__init__()
        self.participant_id = participant_id


class InspectorMessageRequested(Message):
    """A later app integration can open a message action for a participant."""

    def __init__(self, participant_id: str, text: str) -> None:
        super().__init__()
        self.participant_id = participant_id
        self.text = text


class Inspector(Static):
    """Render tabs and content in one bounded drawer widget."""

    can_focus = True

    DEFAULT_CSS = """
    Inspector {
        width: 1fr;
        height: 35%;
        min-height: 4;
        border-top: solid $panel;
        padding: 0 1;
        overflow-y: auto;
    }
    Inspector.-maximized {
        height: 1fr;
    }
    """

    def __init__(self, record: TrajectoryRecord | None = None, **kwargs) -> None:
        super().__init__("", markup=False, **kwargs)
        self._record = record
        self._tabs = tabs_for_record(record)
        self._tab = self._tabs[0]
        self._ratio = 0.35
        self._maximized = False
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
        self.update(f"{heading}\n{content.plain}", layout=False)

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
        if not isinstance(ratio, (int, float)) or isinstance(ratio, bool):
            raise TypeError("inspector ratio must be numeric")
        self._ratio = max(MIN_INSPECTOR_RATIO, min(MAX_INSPECTOR_RATIO, float(ratio)))
        self.styles.height = f"{self._ratio * 100:.2f}%"
        return self._ratio

    def resize_by(self, delta: float) -> float:
        return self.set_ratio(self._ratio + delta)

    def toggle_maximize(self) -> bool:
        self._maximized = not self._maximized
        self.set_class(self._maximized, "-maximized")
        if not self._maximized:
            self.set_ratio(self._ratio)
        return self._maximized

    def emit_participant_link(self, participant_id: str) -> None:
        if participant_id:
            self.post_message(InspectorParticipantLinkClicked(participant_id))

    def emit_message_request(self, participant_id: str, text: str) -> None:
        if participant_id:
            self.post_message(InspectorMessageRequested(participant_id, text[:2048]))

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        event.stop()
        self.resize_by(-0.02)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        event.stop()
        self.resize_by(0.02)


__all__ = [
    "Inspector",
    "InspectorMessageRequested",
    "InspectorParticipantLinkClicked",
    "InspectorTabChanged",
]
