"""Trajectory status, pagination, and mouse-accessible actions."""

from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Button, Label, Select

from theater.regie.trajectory.constants import (
    TRAJECTORY_FOOTER_COMPACT_WIDTH,
    TRAJECTORY_FOOTER_HEIGHT,
    TRAJECTORY_FOOTER_NARROW_WIDTH,
)
from theater.regie.trajectory.enums import DiagnosticView, OrderMode


class FooterActionRequested(Message):
    """A trajectory footer action was clicked."""

    def __init__(self, action: str) -> None:
        super().__init__()
        self.action = action


class FooterPageRequested(Message):
    """A trajectory page was chosen from the footer."""

    def __init__(self, page_index: int) -> None:
        super().__init__()
        self.page_index = page_index


class FooterViewRequested(Message):
    """A trajectory diagnostic view was chosen from the footer."""

    def __init__(self, view: DiagnosticView) -> None:
        super().__init__()
        self.view = view


class TrajectoryFooter(Horizontal):
    """Two-line footer for trajectory navigation and actions."""

    DEFAULT_CSS = f"""
    TrajectoryFooter {{
        width: 1fr;
        height: {TRAJECTORY_FOOTER_HEIGHT};
        min-height: {TRAJECTORY_FOOTER_HEIGHT};
        align-vertical: middle;
        background: $foreground 3%;
        border-top: solid $foreground 12%;
        padding: 0 1;
    }}
    TrajectoryFooter Label {{
        height: 1;
        content-align: left middle;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }}
    TrajectoryFooter #trajectory-page-previous,
    TrajectoryFooter #trajectory-page-next {{
        width: 5;
        min-width: 5;
        padding: 0 1;
        margin-left: 0;
    }}
    TrajectoryFooter #trajectory-page {{
        width: 16;
        min-width: 13;
        height: 1;
        min-height: 1;
        background: $background;
    }}
    TrajectoryFooter #trajectory-page SelectCurrent {{
        height: 1;
        min-height: 1;
        padding: 0 1;
        background: $background;
    }}
    TrajectoryFooter #trajectory-page SelectCurrent Static#label {{
        height: 1;
        content-align: center middle;
        text-style: bold;
    }}
    TrajectoryFooter #trajectory-page SelectCurrent .arrow {{
        padding: 0;
        color: $text-muted;
    }}
    TrajectoryFooter #trajectory-page SelectOverlay {{
        width: 18;
        max-height: 12;
    }}
    TrajectoryFooter #trajectory-page SelectOverlay > .option-list--option {{
        padding: 0 2;
    }}
    TrajectoryFooter #trajectory-view-action {{
        width: 16;
        min-width: 12;
        height: 1;
        min-height: 1;
        margin-left: 1;
        background: $background;
    }}
    TrajectoryFooter #trajectory-view-action SelectCurrent {{
        height: 1;
        min-height: 1;
        padding: 0 1;
        background: $background;
    }}
    TrajectoryFooter #trajectory-view-action SelectCurrent Static#label {{
        height: 1;
        content-align: center middle;
    }}
    TrajectoryFooter #trajectory-view-action SelectCurrent .arrow {{
        padding: 0;
        color: $text-muted;
    }}
    TrajectoryFooter #trajectory-view-action SelectOverlay {{
        width: 18;
        max-height: 14;
    }}
    TrajectoryFooter #trajectory-page-range {{
        width: auto;
        min-width: 14;
        padding-left: 1;
        color: $text-muted;
    }}
    TrajectoryFooter #trajectory-state {{
        width: auto;
        min-width: 10;
        padding-left: 1;
        color: $text-muted;
        text-style: bold;
    }}
    TrajectoryFooter #trajectory-state.-waiting {{ color: $warning; text-opacity: 70%; }}
    TrajectoryFooter #trajectory-state.-problem {{ color: $error; text-opacity: 70%; }}
    TrajectoryFooter #trajectory-status {{
        width: 1fr;
        min-width: 2;
        padding-left: 1;
        color: $text-muted;
    }}
    TrajectoryFooter Button {{
        width: auto;
        min-width: 8;
        height: 1;
        min-height: 1;
        content-align: center middle;
        border: none !important;
        padding: 0 1;
        margin-left: 1;
        color: $text-muted;
        background: $background;
    }}
    TrajectoryFooter Button:hover,
    TrajectoryFooter Button:focus {{
        color: $text;
        background: $accent 10%;
    }}
    TrajectoryFooter Button.-selected {{
        color: $text;
        background: $accent 20%;
    }}
    TrajectoryFooter.-compact Button {{
        min-width: 3;
        padding: 0 1;
        margin-left: 0;
    }}
    TrajectoryFooter.-compact #trajectory-view-action {{
        width: 12;
        min-width: 10;
        margin-left: 0;
    }}
    TrajectoryFooter.-compact #trajectory-state {{ display: none; }}
    TrajectoryFooter.-narrow #trajectory-status {{ display: none; }}
    TrajectoryFooter.-narrow #trajectory-page-range {{ display: none; }}
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._compact = False
        self._active_filters = 0
        self._mode = OrderMode.ORDER
        self._view = DiagnosticView.ALL
        self._follow_tail = True
        self._new_count = 0
        self._page_number = 1
        self._page_count = 1
        self._state_key: tuple[object, ...] | None = None

    def compose(self) -> ComposeResult:
        yield Button("‹", id="trajectory-page-previous", compact=True, flat=True)
        yield Select(
            [("Page 1/1", 0)],
            value=0,
            allow_blank=False,
            id="trajectory-page",
            compact=True,
        )
        yield Button("›", id="trajectory-page-next", compact=True, flat=True)
        yield Label("0 items", id="trajectory-page-range")
        yield Label("● WAITING", id="trajectory-state", classes="-waiting")
        yield Label("Loading…", id="trajectory-status")
        yield Button("⌕ Search", id="trajectory-search-action", compact=True, flat=True)
        yield Button("≡ Filters", id="trajectory-filter-action", compact=True, flat=True)
        yield Button("◷ Duration", id="trajectory-mode-action", compact=True, flat=True)
        yield Select(
            [(view.value.title(), view.value) for view in DiagnosticView],
            value=DiagnosticView.ALL.value,
            allow_blank=False,
            id="trajectory-view-action",
            compact=True,
        )
        yield Button("↓ Live", id="trajectory-follow-action", compact=True, flat=True)

    def _update_actions(self) -> None:
        search = self.query_one("#trajectory-search-action", Button)
        search.label = "⌕" if self._compact else "⌕ Search"
        filters = self.query_one("#trajectory-filter-action", Button)
        filters.label = (
            f"≡{self._active_filters}"
            if self._compact and self._active_filters
            else "≡"
            if self._compact
            else f"≡ Filters {self._active_filters}"
            if self._active_filters
            else "≡ Filters"
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
        mode.set_class(self._mode is OrderMode.DURATION, "-selected")
        view = self.query_one("#trajectory-view-action", Select)
        if view.value != self._view.value:
            view.value = self._view.value
        view.set_class(self._view is not DiagnosticView.ALL, "-selected")
        follow = self.query_one("#trajectory-follow-action", Button)
        follow.label = (
            "↓"
            if self._compact and self._follow_tail
            else f"↓{self._new_count}"
            if self._compact and self._new_count
            else "Ⅱ"
            if self._compact
            else "↓ Live"
            if self._follow_tail
            else f"↓ +{self._new_count}"
            if self._new_count
            else "Ⅱ Paused"
        )
        follow.set_class(self._follow_tail, "-selected")

    def _update_pagination(self) -> None:
        previous = self.query_one("#trajectory-page-previous", Button)
        previous.disabled = self._page_number <= 1
        following = self.query_one("#trajectory-page-next", Button)
        following.disabled = self._page_number >= self._page_count
        selector = self.query_one("#trajectory-page", Select)
        selector.disabled = self._page_count <= 1
        options = [
            (f"Page {page}/{self._page_count}", page - 1) for page in range(1, self._page_count + 1)
        ]
        selector.set_options(options)
        selector.value = self._page_number - 1

    def update_state(
        self,
        *,
        status: str,
        message: str,
        record_count: int,
        visible_count: int,
        page_number: int,
        page_count: int,
        first_item: int,
        last_item: int,
        active_filters: int,
        query: str,
        mode: OrderMode,
        view: DiagnosticView,
        follow_tail: bool,
        new_count: int,
    ) -> None:
        state_key = (
            status,
            message,
            record_count,
            visible_count,
            page_number,
            page_count,
            first_item,
            last_item,
            active_filters,
            query,
            mode,
            view,
            follow_tail,
            new_count,
        )
        if state_key == self._state_key:
            return
        self._state_key = state_key
        state = self.query_one("#trajectory-state", Label)
        state.update(f"● {status.upper()}")
        state.set_class(status == "waiting", "-waiting")
        state.set_class(status in {"untrusted", "unavailable", "stale"}, "-problem")
        page_count_changed = page_count != self._page_count
        self._page_number = page_number
        self._page_count = page_count
        selector = self.query_one("#trajectory-page", Select)
        if page_count_changed:
            self._update_pagination()
        else:
            selector.value = page_number - 1
            self.query_one("#trajectory-page-previous", Button).disabled = page_number <= 1
            self.query_one("#trajectory-page-next", Button).disabled = page_number >= page_count
        item_range = (
            "0 items" if not visible_count else f"{first_item}–{last_item}/{visible_count} items"
        )
        self.query_one("#trajectory-page-range", Label).update(item_range)
        details = [f"{record_count} loaded"]
        if message:
            details.append(message)
        if query:
            details.append(f"search: {query}")
        status_text = " · ".join(details)
        status_label = self.query_one("#trajectory-status", Label)
        status_label.update(status_text)
        search = self.query_one("#trajectory-search-action", Button)
        search.set_class(bool(query), "-selected")
        self._active_filters = active_filters
        self._mode = mode
        self._view = view
        self._follow_tail = follow_tail
        self._new_count = new_count
        self._update_actions()

    def on_resize(self, event: events.Resize) -> None:
        compact = event.size.width < TRAJECTORY_FOOTER_COMPACT_WIDTH
        narrow = event.size.width < TRAJECTORY_FOOTER_NARROW_WIDTH
        changed = compact != self._compact
        self._compact = compact
        self.set_class(compact, "-compact")
        self.set_class(narrow, "-narrow")
        if changed and self.is_mounted:
            self._update_actions()

    def on_button_pressed(self, message: Button.Pressed) -> None:
        actions = {
            "trajectory-page-previous": "previous_page",
            "trajectory-page-next": "next_page",
            "trajectory-search-action": "search",
            "trajectory-filter-action": "filters",
            "trajectory-mode-action": "mode",
            "trajectory-follow-action": "follow",
        }
        action = actions.get(message.button.id or "")
        if action is None:
            return
        message.stop()
        self.post_message(FooterActionRequested(action))

    def on_select_changed(self, message: Select.Changed) -> None:
        if message.select.id == "trajectory-view-action":
            value = message.value
            if not isinstance(value, str) or value == self._view.value:
                return
            message.stop()
            self.post_message(FooterViewRequested(DiagnosticView(value)))
            return
        page_index = message.value
        if not isinstance(page_index, int) or isinstance(page_index, bool):
            return
        message.stop()
        if page_index != message.select.value or page_index == self._page_number - 1:
            return
        self.post_message(FooterPageRequested(page_index))


__all__ = [
    "FooterActionRequested",
    "FooterPageRequested",
    "FooterViewRequested",
    "TrajectoryFooter",
]
