"""Contextual tabbed inspector for one bounded trajectory record."""

from __future__ import annotations

from math import isfinite

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Label, RichLog, TabbedContent, TabPane

from theater.regie.trajectory.constants import (
    INSPECTOR_HEADER_HEIGHT,
    INSPECTOR_LINKS_HEIGHT,
    INSPECTOR_MIN_HEIGHT,
    INSPECTOR_RESIZE_STEP,
    INSPECTOR_SCROLL_STEP,
    TRAJECTORY_INSPECTOR_RATIO_DEFAULT,
    TRAJECTORY_INSPECTOR_RATIO_MAX,
    TRAJECTORY_INSPECTOR_RATIO_MIN,
)
from theater.regie.trajectory.enums import InspectorTab
from theater.regie.trajectory.render import (
    format_duration,
    inspector_content,
    tabs_for_record,
)
from theater.trajectory import LinkDirection, TrajectoryRecord


class InspectorTabChanged(Message):
    """The active contextual inspector tab changed."""

    def __init__(self, tab: InspectorTab) -> None:
        super().__init__()
        self.tab = tab


class InspectorParticipantLinkClicked(Message):
    """A participant link was activated."""

    def __init__(self, participant_id: str) -> None:
        super().__init__()
        self.participant_id = participant_id


class InspectorResizeRequested(Message):
    """A deliberate gesture requested a drawer resize."""

    def __init__(self, delta: float) -> None:
        super().__init__()
        self.delta = delta


class InspectorMaximizeRequested(Message):
    """A control requested maximize or restore."""


class InspectorHandle(Label):
    """Mouse resize handle for the inspector drawer."""

    DEFAULT_CSS = """
    InspectorHandle {
        width: 1fr;
        height: 1;
        color: $foreground 28%;
        background: $panel;
        text-align: center;
        pointer: ns-resize;
    }
    InspectorHandle:hover {
        color: $accent;
        background: $accent 8%;
    }
    """

    def __init__(self) -> None:
        super().__init__("━━━━━━━━━━", id="trajectory-inspector-handle")
        self._resizing = False

    def on_click(self, event: events.Click) -> None:
        if event.chain >= 2:
            event.stop()
            self.post_message(InspectorMaximizeRequested())

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1:
            return
        event.stop()
        self._resizing = True
        self.capture_mouse()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._resizing:
            return
        event.stop()
        inspector = self.parent
        host = inspector.parent if isinstance(inspector, Widget) else None
        height = host.region.height if isinstance(host, Widget) else 0
        if height:
            self.post_message(InspectorResizeRequested(-event.delta_y / height))

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if not self._resizing:
            return
        event.stop()
        self._resizing = False
        self.release_mouse()


class Inspector(Vertical):
    """Present bounded details through native tabs and scrollable content."""

    can_focus = True

    DEFAULT_CSS = f"""
    Inspector {{
        width: 1fr;
        height: {TRAJECTORY_INSPECTOR_RATIO_DEFAULT * 100:.0f}%;
        min-height: {INSPECTOR_MIN_HEIGHT};
        background: $surface;
        border-top: solid $accent 35%;
    }}
    Inspector.-maximized {{
        height: 1fr;
    }}
    Inspector > #trajectory-inspector-header {{
        width: 1fr;
        height: {INSPECTOR_HEADER_HEIGHT};
        min-height: {INSPECTOR_HEADER_HEIGHT};
        align-vertical: middle;
        padding: 0 1;
        background: $panel;
    }}
    Inspector #trajectory-inspector-title {{
        width: 1fr;
        height: {INSPECTOR_HEADER_HEIGHT};
        content-align: left middle;
        color: $text;
        text-style: bold;
        text-overflow: ellipsis;
        text-wrap: nowrap;
    }}
    Inspector #trajectory-inspector-duration {{
        width: auto;
        min-width: 8;
        height: {INSPECTOR_HEADER_HEIGHT};
        content-align: right middle;
        color: $text-muted;
        text-align: right;
        padding: 0 1;
    }}
    Inspector #trajectory-inspector-maximize {{
        width: auto;
        min-width: 10;
        height: {INSPECTOR_HEADER_HEIGHT};
        content-align: center middle;
        border: none !important;
        color: $text-muted;
        background: $surface;
    }}
    Inspector #trajectory-inspector-maximize:hover,
    Inspector #trajectory-inspector-maximize:focus {{
        color: $text;
        background: $accent 20%;
    }}
    Inspector TabbedContent {{
        width: 1fr;
        height: 1fr;
        min-height: 2;
    }}
    Inspector ContentTabs {{
        height: 3;
        background: $panel;
    }}
    Inspector Tab {{
        height: 3;
        padding: 0 1;
        content-align: center middle;
    }}
    Inspector TabPane {{
        padding: 0;
    }}
    Inspector RichLog {{
        width: 1fr;
        height: 1fr;
        padding: 0 1;
        background: $surface;
        scrollbar-size: 1 1;
    }}
    Inspector > #trajectory-inspector-links {{
        display: none;
        width: 1fr;
        height: {INSPECTOR_LINKS_HEIGHT};
        min-height: {INSPECTOR_LINKS_HEIGHT};
        padding: 0 1;
        align-vertical: middle;
        background: $panel;
        overflow-x: auto;
    }}
    Inspector > #trajectory-inspector-links.-visible {{
        display: block;
    }}
    Inspector #trajectory-inspector-links-label {{
        width: auto;
        min-width: 9;
        height: {INSPECTOR_LINKS_HEIGHT};
        content-align: left middle;
        color: $text-muted;
    }}
    Inspector .trajectory-participant-link {{
        width: auto;
        min-width: 12;
        height: {INSPECTOR_LINKS_HEIGHT};
        content-align: center middle;
        border: none !important;
        margin-right: 1;
        color: $text-muted;
        background: $surface;
    }}
    Inspector .trajectory-participant-link:hover,
    Inspector .trajectory-participant-link:focus {{
        color: $text;
        background: $accent 20%;
    }}
    """

    def __init__(self, record: TrajectoryRecord | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._record = record
        self._tabs = tabs_for_record(record)
        self._tab = self._tabs[0]
        self._ratio = TRAJECTORY_INSPECTOR_RATIO_DEFAULT
        self._maximized = False
        self._button_links: dict[str, str] = {}
        self._link_buttons: list[Button] = []
        self._syncing_tabs = False
        self._rendered_state: tuple[TrajectoryRecord | None, InspectorTab] | None = None

    @staticmethod
    def _pane_id(tab: InspectorTab) -> str:
        return f"trajectory-inspector-tab-{tab.value}"

    @staticmethod
    def _log_id(tab: InspectorTab) -> str:
        return f"trajectory-inspector-content-{tab.value}"

    def compose(self) -> ComposeResult:
        yield InspectorHandle()
        with Horizontal(id="trajectory-inspector-header"):
            yield Label("No event selected", id="trajectory-inspector-title")
            yield Label("—", id="trajectory-inspector-duration")
            yield Button(
                "□ Maximize",
                id="trajectory-inspector-maximize",
                compact=True,
                flat=True,
            )
        with TabbedContent(id="trajectory-inspector-tabs"):
            for tab in InspectorTab:
                with TabPane(tab.value.replace("_", " ").title(), id=self._pane_id(tab)):
                    yield RichLog(
                        id=self._log_id(tab),
                        wrap=True,
                        markup=False,
                        highlight=False,
                    )
        with Horizontal(id="trajectory-inspector-links"):
            yield Label("RELATED", id="trajectory-inspector-links-label")
            links = self._record.links if self._record is not None else ()
            for index in range(len(links)):
                yield Button(
                    "",
                    id=f"trajectory-participant-link-{index}",
                    classes="trajectory-participant-link",
                    compact=True,
                    flat=True,
                )

    def on_mount(self) -> None:
        self._link_buttons = list(self.query(".trajectory-participant-link").results(Button))
        self._render_content()
        self._apply_height()

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

    def _title(self) -> Text:
        if self._record is None:
            return Text("No event selected", style="dim")
        title = Text()
        title.append(self._record.kind.value.replace("_", " ").upper(), style="bold cyan")
        title.append(f"  {self._record.source}", style="bold")
        title.append(f"  {self._record.status.value.replace('_', ' ')}", style="dim")
        return title

    def _content_text(self, tab: InspectorTab) -> Text:
        content = inspector_content(self._record, tab)
        lines = content.plain.splitlines()
        rendered = Text()
        for index, line in enumerate(lines):
            if index:
                rendered.append("\n")
            if index == 0:
                rendered.append(line, style="bold")
            elif line.startswith("No "):
                rendered.append(line, style="dim italic")
            elif ":" in line and not line.lstrip().startswith(("{", "[", '"')):
                key, value = line.split(":", 1)
                rendered.append(f"{key}:", style="bold cyan")
                rendered.append(value)
            else:
                rendered.append(line)
        return rendered

    def _sync_tab_visibility(self) -> None:
        if not self.is_mounted:
            return
        tabbed = self.query_one("#trajectory-inspector-tabs", TabbedContent)
        for candidate in InspectorTab:
            candidate_tab = tabbed.get_tab(self._pane_id(candidate))
            visible = candidate in self._tabs
            if candidate_tab.display != visible:
                candidate_tab.display = visible
        self._syncing_tabs = True
        try:
            pane_id = self._pane_id(self._tab)
            if tabbed.active != pane_id:
                tabbed.active = pane_id
        finally:
            self._syncing_tabs = False

    def _sync_links(self) -> None:
        if not self.is_mounted:
            return
        links = self._record.links if self._record is not None else ()
        self._button_links.clear()
        container = self.query_one("#trajectory-inspector-links", Horizontal)
        container.set_class(bool(links), "-visible")
        if len(self._link_buttons) < len(links):
            new_buttons = [
                Button(
                    "",
                    id=f"trajectory-participant-link-{index}",
                    classes="trajectory-participant-link",
                    compact=True,
                    flat=True,
                )
                for index in range(len(self._link_buttons), len(links))
            ]
            self._link_buttons.extend(new_buttons)
            container.mount(*new_buttons)
        for index, button in enumerate(self._link_buttons):
            if index >= len(links):
                if button.display:
                    button.display = False
                continue
            link = links[index]
            direction = {
                LinkDirection.INCOMING: "←",
                LinkDirection.OUTGOING: "→",
                LinkDirection.RELATED: "↔",
            }[link.direction]
            button.label = f"{direction} {link.relation}: {link.participant_id}"
            button.display = True
            if button.id is not None:
                self._button_links[button.id] = link.participant_id

    def _render_tab(self, tab: InspectorTab) -> None:
        log = self.query_one(f"#{self._log_id(tab)}", RichLog)
        log.clear()
        log.write(self._content_text(tab), scroll_end=False)
        log.scroll_home(animate=False)

    def _render_content(self) -> None:
        if not self.is_mounted:
            return
        self.query_one("#trajectory-inspector-title", Label).update(self._title())
        self.query_one("#trajectory-inspector-duration", Label).update(
            format_duration(self._record.timing) if self._record is not None else "—"
        )
        self._sync_tab_visibility()
        self._render_tab(self._tab)
        self._sync_links()
        self._rendered_state = (self._record, self._tab)

    def set_record(
        self,
        record: TrajectoryRecord | None,
        *,
        tab: InspectorTab | None = None,
        render: bool = True,
    ) -> None:
        self._record = record
        self._tabs = tabs_for_record(record)
        self._tab = tab if tab in self._tabs else self._tabs[0]
        if render and self._rendered_state != (self._record, self._tab):
            self._render_content()

    def set_tab(self, tab: InspectorTab) -> InspectorTab:
        if tab not in self._tabs or tab is self._tab:
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
        ratio = max(
            TRAJECTORY_INSPECTOR_RATIO_MIN,
            min(TRAJECTORY_INSPECTOR_RATIO_MAX, float(ratio)),
        )
        if ratio == self._ratio:
            return self._ratio
        self._ratio = ratio
        self._apply_height()
        return self._ratio

    def resize_by(self, delta: float) -> float:
        return self.set_ratio(self._ratio + delta)

    def toggle_maximize(self) -> bool:
        self._maximized = not self._maximized
        self.set_class(self._maximized, "-maximized")
        self._apply_height()
        if self.is_mounted:
            button = self.query_one("#trajectory-inspector-maximize", Button)
            button.label = "▣ Restore" if self._maximized else "□ Maximize"
        return self._maximized

    def _apply_height(self) -> None:
        self.styles.height = "1fr" if self._maximized else f"{self._ratio * 100:.2f}%"

    def emit_participant_link(self, participant_id: str) -> None:
        if participant_id:
            self.post_message(InspectorParticipantLinkClicked(participant_id))

    def on_tabbed_content_tab_activated(self, message: TabbedContent.TabActivated) -> None:
        if (
            self._syncing_tabs
            or message.tabbed_content.id != "trajectory-inspector-tabs"
            or message.pane.id is None
        ):
            return
        value = message.pane.id.removeprefix("trajectory-inspector-tab-")
        try:
            tab = InspectorTab(value)
        except ValueError:
            return
        if tab in self._tabs and tab is not self._tab:
            self._tab = tab
            self._render_tab(tab)
            self._rendered_state = (self._record, self._tab)
            self.post_message(InspectorTabChanged(tab))

    def on_button_pressed(self, message: Button.Pressed) -> None:
        if message.button.id == "trajectory-inspector-maximize":
            self.post_message(InspectorMaximizeRequested())
        elif participant_id := self._button_links.get(message.button.id or ""):
            self.emit_participant_link(participant_id)
        else:
            return
        message.stop()

    def on_key(self, event: events.Key) -> None:
        if event.key in {"ctrl+left", "ctrl+up"}:
            event.stop()
            self.post_message(InspectorResizeRequested(-INSPECTOR_RESIZE_STEP))
        elif event.key in {"ctrl+right", "ctrl+down"}:
            event.stop()
            self.post_message(InspectorResizeRequested(INSPECTOR_RESIZE_STEP))

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        event.stop()
        if event.shift:
            self.post_message(InspectorResizeRequested(-INSPECTOR_RESIZE_STEP))
            return
        if self.is_mounted:
            self.query_one(f"#{self._log_id(self._tab)}", RichLog).scroll_relative(
                y=-INSPECTOR_SCROLL_STEP, animate=False
            )

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        event.stop()
        if event.shift:
            self.post_message(InspectorResizeRequested(INSPECTOR_RESIZE_STEP))
            return
        if self.is_mounted:
            self.query_one(f"#{self._log_id(self._tab)}", RichLog).scroll_relative(
                y=INSPECTOR_SCROLL_STEP, animate=False
            )


__all__ = [
    "Inspector",
    "InspectorMaximizeRequested",
    "InspectorParticipantLinkClicked",
    "InspectorResizeRequested",
    "InspectorTabChanged",
]
