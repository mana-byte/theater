"""Full-ledger detail panel for one trajectory span or tool operation."""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Label, RichLog, TabbedContent, TabPane

from theater.regie.trajectory.enums import InspectorTab
from theater.regie.trajectory.inspection.links import (
    DETAIL_PARTICIPANT_EXACT_META,
    DETAIL_PARTICIPANT_META,
    DETAIL_PARTICIPANT_UNRESOLVED_META,
    DETAIL_RECORD_TARGET_META,
    participant_link_from_meta,
)
from theater.regie.trajectory.inspection.styled import (
    SpanDetails,
    build_span_details,
    build_tool_span_details,
)
from theater.regie.trajectory.render.records import format_duration, sanitize_text
from theater.trajectory import ParticipantLink, TrajectoryRecord, TrajectoryRequest
from theater.trajectory.tools import TrajectoryToolOperation


class SpanDetailClosed(Message):
    """The detail panel requested a return to the span list."""


class SpanDetailTabChanged(Message):
    """The active contextual detail tab changed."""

    def __init__(self, tab: InspectorTab) -> None:
        super().__init__()
        self.tab = tab


class SpanDetailParticipantLinkClicked(Message):
    """A participant link in the detail content was activated."""

    def __init__(self, link: ParticipantLink, *, exact: bool, unresolved: bool) -> None:
        super().__init__()
        self.link = link
        self.participant_id = link.participant_id
        self.target_record_id = link.target_record_id
        self.exact = exact
        self.unresolved = unresolved


class SpanDetailRecordLinkClicked(Message):
    """A record link in the detail content was activated."""

    def __init__(self, record_id: str) -> None:
        super().__init__()
        self.record_id = record_id


class SpanDetailPanel(Vertical):
    """Replace the ledger with bounded, tabbed, scrollable span details."""

    can_focus = True
    COMPONENT_CLASSES: ClassVar[set[str]] = Widget.COMPONENT_CLASSES | {"span-detail--accent"}

    DEFAULT_CSS = """
    SpanDetailPanel {
        width: 1fr;
        height: 1fr;
        min-height: 0;
        background: $background;
        border-top: solid $accent 30%;
    }
    SpanDetailPanel > #trajectory-span-detail-header {
        width: 1fr;
        height: 3;
        min-height: 3;
        padding: 0 1;
        align-vertical: middle;
        background: $foreground 3%;
    }
    SpanDetailPanel #trajectory-span-detail-title {
        width: 1fr;
        height: 3;
        content-align: left middle;
        text-overflow: ellipsis;
        text-wrap: nowrap;
    }
    SpanDetailPanel #trajectory-span-detail-duration {
        width: auto;
        min-width: 8;
        height: 3;
        padding: 0 1;
        content-align: right middle;
        color: $text-muted;
    }
    SpanDetailPanel #trajectory-span-detail-close {
        width: auto;
        min-width: 13;
        height: 3;
        border: none !important;
        color: $text-muted;
        background: $background;
    }
    SpanDetailPanel #trajectory-span-detail-close.-style-flat:hover,
    SpanDetailPanel #trajectory-span-detail-close.-style-flat:focus {
        color: $text;
        background: $accent 15%;
        tint: transparent;
    }
    SpanDetailPanel #trajectory-span-detail-close.-style-flat.-active {
        color: $text;
        background: $accent 20%;
        tint: transparent;
    }
    SpanDetailPanel > #trajectory-span-detail-tabs {
        width: 1fr;
        height: 1fr;
        min-height: 0;
    }
    SpanDetailPanel ContentTabs {
        height: 3;
        background: $foreground 3%;
    }
    SpanDetailPanel Tab {
        height: 3;
        padding: 0 1;
        content-align: center middle;
    }
    SpanDetailPanel Tab:hover {
        color: $text;
        background: $accent 10%;
    }
    SpanDetailPanel Tab.-active,
    SpanDetailPanel Tabs:focus Tab.-active {
        color: $text;
        background: $accent 20%;
        text-style: bold;
    }
    SpanDetailPanel Tabs:focus .underline--bar {
        background: $accent 30%;
    }
    SpanDetailPanel TabPane {
        padding: 0;
    }
    SpanDetailPanel RichLog {
        width: 1fr;
        height: 1fr;
        padding: 1 2;
        background: $background;
        scrollbar-size: 1 1;
    }
    SpanDetailPanel > .span-detail--accent {
        color: $accent;
        text-style: dim;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._record: TrajectoryRecord | None = None
        self._tool: TrajectoryToolOperation | None = None
        self._request: TrajectoryRequest | None = None
        self._details: SpanDetails | None = None
        self._syncing_tabs = False

    @staticmethod
    def _pane_id(tab: InspectorTab) -> str:
        return f"trajectory-span-detail-tab-{tab.value}"

    @staticmethod
    def _log_id(tab: InspectorTab) -> str:
        return f"trajectory-span-detail-content-{tab.value}"

    def compose(self) -> ComposeResult:
        with Horizontal(id="trajectory-span-detail-header"):
            yield Label("No span selected", id="trajectory-span-detail-title")
            yield Label("—", id="trajectory-span-detail-duration")
            yield Button("← Quit span", id="trajectory-span-detail-close", compact=True, flat=True)
        with TabbedContent(id="trajectory-span-detail-tabs"):
            for tab in InspectorTab:
                with TabPane(tab.value.replace("_", " ").title(), id=self._pane_id(tab)):
                    yield RichLog(id=self._log_id(tab), wrap=True, markup=False, highlight=False)

    @property
    def record_id(self) -> str | None:
        return self._record.record_id if self._record is not None else None

    @property
    def tab(self) -> InspectorTab:
        return self._details.tab if self._details is not None else InspectorTab.SUMMARY

    @property
    def tabs(self) -> tuple[InspectorTab, ...]:
        return self._details.tabs if self._details is not None else (InspectorTab.SUMMARY,)

    @property
    def copy_text(self) -> str:
        return self._details.copy_text if self._details is not None else ""

    def _build_details(self, tab: InspectorTab) -> SpanDetails | None:
        accent = self.get_component_rich_style("span-detail--accent", partial=True)
        if self._tool is not None:
            return build_tool_span_details(self._tool, tab, accent_style=accent)
        if self._record is not None:
            return build_span_details(self._record, tab, accent_style=accent, request=self._request)
        return None

    def _title(self) -> Text:
        if self._record is None:
            return Text("No span selected", style="dim")
        title = Text()
        if self._tool is not None:
            title.append("TOOL", style="bold")
            title.append(f"  {sanitize_text(self._tool.tool_name or 'unknown')}")
            source = self._tool.source
            status = self._tool.status.value
        else:
            title.append(self._record.kind.value.replace("_", " ").upper(), style="bold")
            source = self._record.source
            status = self._record.status.value
        title.append(f"  {sanitize_text(source)}", style="dim")
        title.append(f"  {status.replace('_', ' ')}", style="dim")
        return title

    def _sync_tabs(self) -> None:
        if not self.is_mounted or self._details is None:
            return
        tabs = self.query_one("#trajectory-span-detail-tabs", TabbedContent)
        for candidate in InspectorTab:
            tabs.get_tab(self._pane_id(candidate)).display = candidate in self._details.tabs
        self._syncing_tabs = True
        try:
            pane_id = self._pane_id(self._details.tab)
            if tabs.active != pane_id:
                tabs.active = pane_id
        finally:
            self._syncing_tabs = False

    def _render_content(self, scroll_y: float | None = None) -> None:
        if not self.is_mounted:
            return
        self.query_one("#trajectory-span-detail-title", Label).update(self._title())
        timing = (
            self._tool.timing
            if self._tool is not None
            else self._record.timing
            if self._record is not None
            else None
        )
        self.query_one("#trajectory-span-detail-duration", Label).update(format_duration(timing))
        self._sync_tabs()
        if self._details is None:
            return
        log = self.query_one(f"#{self._log_id(self._details.tab)}", RichLog)
        log.clear()
        log.write(self._details.content, scroll_end=False)
        if scroll_y is None:
            log.scroll_home(animate=False)
        else:
            log.scroll_to(y=scroll_y, animate=False, force=True)

    def set_span(
        self,
        record: TrajectoryRecord,
        *,
        tool: TrajectoryToolOperation | None = None,
        request: TrajectoryRequest | None = None,
        tab: InspectorTab = InspectorTab.SUMMARY,
    ) -> InspectorTab:
        if (
            self._record == record
            and self._tool == tool
            and self._request == request
            and self._details is not None
            and tab is self._details.tab
        ):
            return self.tab
        preserve_scroll = (
            self._record is not None
            and self._record.record_id == record.record_id
            and self._details is not None
            and tab is self._details.tab
        )
        scroll_y = (
            float(self.query_one(f"#{self._log_id(self._details.tab)}", RichLog).scroll_y)
            if preserve_scroll and self.is_mounted and self._details is not None
            else None
        )
        self._record = record
        self._tool = tool
        self._request = request
        self._details = self._build_details(tab)
        self._render_content(scroll_y)
        if self.is_mounted:
            self.call_after_refresh(self._render_content, scroll_y)
        return self.tab

    def set_tab(self, tab: InspectorTab) -> InspectorTab:
        if self._record is None or tab not in self.tabs or tab is self.tab:
            return self.tab
        self._details = self._build_details(tab)
        self._render_content()
        self.post_message(SpanDetailTabChanged(self.tab))
        return self.tab

    def move_tab(self, delta: int) -> InspectorTab:
        tabs = self.tabs
        index = tabs.index(self.tab)
        return self.set_tab(tabs[max(0, min(len(tabs) - 1, index + delta))])

    def scroll_content(self, delta: int) -> None:
        if not self.is_mounted or self._details is None:
            return
        self.query_one(f"#{self._log_id(self._details.tab)}", RichLog).scroll_relative(
            y=delta, animate=False
        )

    def on_tabbed_content_tab_activated(self, message: TabbedContent.TabActivated) -> None:
        if (
            self._syncing_tabs
            or message.tabbed_content.id != "trajectory-span-detail-tabs"
            or message.pane.id is None
        ):
            return
        try:
            tab = InspectorTab(message.pane.id.removeprefix("trajectory-span-detail-tab-"))
        except ValueError:
            return
        if tab in self.tabs and tab is not self.tab:
            self.set_tab(tab)

    def on_button_pressed(self, message: Button.Pressed) -> None:
        if message.button.id != "trajectory-span-detail-close":
            return
        message.stop()
        self.post_message(SpanDetailClosed())

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1:
            return
        meta = event.style.meta
        if link := participant_link_from_meta(meta):
            event.stop()
            self.post_message(
                SpanDetailParticipantLinkClicked(
                    link,
                    exact=meta.get(DETAIL_PARTICIPANT_EXACT_META) == "1",
                    unresolved=meta.get(DETAIL_PARTICIPANT_UNRESOLVED_META) == "1",
                )
            )
            return
        participant_id = meta.get(DETAIL_PARTICIPANT_META)
        if isinstance(participant_id, str):
            event.stop()
            self.post_message(
                SpanDetailParticipantLinkClicked(
                    ParticipantLink(participant_id, "related"), exact=False, unresolved=False
                )
            )
            return
        record_id = meta.get(DETAIL_RECORD_TARGET_META)
        if isinstance(record_id, str):
            event.stop()
            self.post_message(SpanDetailRecordLinkClicked(record_id))


__all__ = [
    "SpanDetailClosed",
    "SpanDetailPanel",
    "SpanDetailParticipantLinkClicked",
    "SpanDetailRecordLinkClicked",
    "SpanDetailTabChanged",
]
