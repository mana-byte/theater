"""Trajectory title, state, and mouse-accessible actions."""

from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Button, Label

from theater.regie.trajectory.constants import (
    TOOLBAR_COMPACT_WIDTH,
    TOOLBAR_HEIGHT,
    TOOLBAR_NARROW_WIDTH,
)
from theater.regie.trajectory.enums import OrderMode


class ToolbarActionRequested(Message):
    """A trajectory toolbar action was clicked."""

    def __init__(self, action: str) -> None:
        super().__init__()
        self.action = action


class TrajectoryToolbar(Horizontal):
    """Compact native controls for trajectory state and actions."""

    DEFAULT_CSS = f"""
    TrajectoryToolbar {{
        width: 1fr;
        height: {TOOLBAR_HEIGHT};
        min-height: {TOOLBAR_HEIGHT};
        align-vertical: middle;
        background: $panel;
        border-bottom: solid $foreground 12%;
        padding: 0 1;
    }}
    TrajectoryToolbar #trajectory-title {{
        width: auto;
        min-width: 12;
        height: 3;
        content-align: left middle;
        color: $text;
        text-style: bold;
    }}
    TrajectoryToolbar #trajectory-state {{
        width: auto;
        min-width: 10;
        height: 3;
        content-align: left middle;
        padding: 0 1;
        color: $text-success;
        text-style: bold;
    }}
    TrajectoryToolbar #trajectory-state.-waiting {{
        color: $text-warning;
    }}
    TrajectoryToolbar #trajectory-state.-problem {{
        color: $text-error;
    }}
    TrajectoryToolbar #trajectory-status {{
        width: 1fr;
        min-width: 8;
        height: 3;
        content-align: left middle;
        color: $text-muted;
        text-overflow: ellipsis;
        text-wrap: nowrap;
    }}
    TrajectoryToolbar Button {{
        min-width: 8;
        width: auto;
        height: 3;
        content-align: center middle;
        border: none !important;
        margin-left: 1;
        color: $text-muted;
        background: $surface;
    }}
    TrajectoryToolbar Button:hover,
    TrajectoryToolbar Button:focus {{
        color: $text;
        background: $accent 20%;
    }}
    TrajectoryToolbar Button.-selected {{
        color: $text;
        background: $accent 25%;
    }}
    TrajectoryToolbar.-compact #trajectory-title {{
        display: none;
    }}
    TrajectoryToolbar.-compact Button {{
        min-width: 6;
    }}
    TrajectoryToolbar.-narrow #trajectory-status,
    TrajectoryToolbar.-narrow #trajectory-search-action {{
        display: none;
    }}
    TrajectoryToolbar.-narrow #trajectory-state {{
        width: 1fr;
    }}
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._compact = False
        self._active_filters = 0
        self._mode = OrderMode.ORDER
        self._follow_tail = True
        self._new_count = 0
        self._state_key: tuple[object, ...] | None = None

    def compose(self) -> ComposeResult:
        yield Label("TRAJECTORY", id="trajectory-title")
        yield Label("● WAITING", id="trajectory-state", classes="-waiting")
        yield Label("Loading…", id="trajectory-status")
        yield Button("⌕ Search", id="trajectory-search-action", compact=True, flat=True)
        yield Button("≡ Filters", id="trajectory-filter-action", compact=True, flat=True)
        yield Button("◷ Duration", id="trajectory-mode-action", compact=True, flat=True)
        yield Button("↓ Live", id="trajectory-follow-action", compact=True, flat=True)

    def _update_actions(self) -> None:
        filters = self.query_one("#trajectory-filter-action", Button)
        filters.label = (
            f"≡ {self._active_filters}"
            if self._compact and self._active_filters
            else "≡"
            if self._compact
            else f"≡ Filters {self._active_filters}"
            if self._active_filters
            else "≡ Filters"
        )
        filters.tooltip = (
            f"Filters ({self._active_filters} active)" if self._active_filters else "Filters"
        )
        filters.set_class(self._active_filters > 0, "-selected")
        mode = self.query_one("#trajectory-mode-action", Button)
        mode.label = (
            "≡"
            if self._compact and self._mode is OrderMode.DURATION
            else "◷"
            if self._compact
            else "≡ Order"
            if self._mode is OrderMode.DURATION
            else "◷ Duration"
        )
        mode.tooltip = (
            "Switch to event order"
            if self._mode is OrderMode.DURATION
            else "Switch to recorded duration"
        )
        mode.set_class(self._mode is OrderMode.DURATION, "-selected")
        follow = self.query_one("#trajectory-follow-action", Button)
        follow.label = (
            "↓ Live"
            if self._follow_tail
            else f"↓ +{self._new_count}"
            if self._new_count
            else "↓ Paused"
        )
        follow.tooltip = "Following live events" if self._follow_tail else "Resume live tail"
        follow.set_class(self._follow_tail, "-selected")

    def update_state(
        self,
        *,
        status: str,
        message: str,
        record_count: int,
        visible_count: int,
        active_filters: int,
        query: str,
        mode: OrderMode,
        follow_tail: bool,
        new_count: int,
    ) -> None:
        state_key = (
            status,
            message,
            record_count,
            visible_count,
            active_filters,
            query,
            mode,
            follow_tail,
            new_count,
        )
        if state_key == self._state_key:
            return
        self._state_key = state_key
        state = self.query_one("#trajectory-state", Label)
        state.update(f"● {status.upper()}")
        state.set_class(status in {"waiting"}, "-waiting")
        state.set_class(status in {"untrusted", "unavailable", "stale"}, "-problem")
        count = (
            f"{visible_count}/{record_count}"
            if visible_count != record_count
            else str(record_count)
        )
        details = [f"{count} events"]
        if message:
            details.append(message)
        if query:
            details.append(f"search: {query}")
        status_label = self.query_one("#trajectory-status", Label)
        status_label.update(" · ".join(details))
        status_label.tooltip = " · ".join(details)

        search = self.query_one("#trajectory-search-action", Button)
        search.set_class(bool(query), "-selected")
        search.tooltip = "Focus trajectory search"
        self._active_filters = active_filters
        self._mode = mode
        self._follow_tail = follow_tail
        self._new_count = new_count
        self._update_actions()

    def on_resize(self, event: events.Resize) -> None:
        compact = event.size.width < TOOLBAR_COMPACT_WIDTH
        narrow = event.size.width < TOOLBAR_NARROW_WIDTH
        changed = compact != self._compact
        self._compact = compact
        self.set_class(compact, "-compact")
        self.set_class(narrow, "-narrow")
        if changed and self.is_mounted:
            self._update_actions()

    def on_button_pressed(self, message: Button.Pressed) -> None:
        actions = {
            "trajectory-search-action": "search",
            "trajectory-filter-action": "filters",
            "trajectory-mode-action": "mode",
            "trajectory-follow-action": "follow",
        }
        action = actions.get(message.button.id or "")
        if action is None:
            return
        message.stop()
        self.post_message(ToolbarActionRequested(action))


__all__ = ["ToolbarActionRequested", "TrajectoryToolbar"]
