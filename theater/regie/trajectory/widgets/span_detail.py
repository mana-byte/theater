"""Full-ledger detail panel for one trajectory span or tool operation."""

from __future__ import annotations

from typing import ClassVar

from rich.segment import Segment
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.selection import Selection
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Button, Label, LoadingIndicator, RichLog, TabbedContent, TabPane

from theater.observability import span
from theater.observability.catalog import (
    REGIE_TRAJECTORY_DETAIL_PROJECT,
    REGIE_TRAJECTORY_DETAIL_RENDER,
)
from theater.regie.trajectory.enums import InspectorTab
from theater.regie.trajectory.inspection.links import (
    DETAIL_JSON_TOGGLE_META,
    DETAIL_PARTICIPANT_EXACT_META,
    DETAIL_PARTICIPANT_META,
    DETAIL_PARTICIPANT_UNRESOLVED_META,
    DETAIL_RECORD_TARGET_META,
    participant_link_from_meta,
)
from theater.regie.trajectory.inspection.rich_content import DetailStyles
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


class _DetailLoadingIndicator(LoadingIndicator):
    def _on_mount(self, event: events.Mount) -> None:
        super()._on_mount(event)
        self.auto_refresh = None

    def set_active(self, active: bool) -> None:
        self.display = active
        self.auto_refresh = 1 / 16 if active else None


class _SelectableRichLog(RichLog):
    """RichLog with Textual's missing selection extraction and offsets."""

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        text = "\n".join(line.text.rstrip(" ") for line in self.lines)
        return selection.extract(text), "\n"

    def render_line(self, y: int) -> Strip:
        scroll_x, scroll_y = map(int, self.scroll_offset)
        content_y = scroll_y + y
        width = self.scrollable_content_region.width
        line = self._render_line(content_y, scroll_x, width).apply_style(self.rich_style)
        selection = self.text_selection
        if selection is not None and (span := selection.get_span(content_y)) is not None:
            start, end = span
            visible_start = max(scroll_x, start)
            visible_end = scroll_x + line.cell_length if end == -1 else min(scroll_x + width, end)
            if visible_start < visible_end:
                local_start = visible_start - scroll_x
                local_end = visible_end - scroll_x
                selection_style = self.screen.get_component_rich_style("screen--selection")
                selected = line.crop(local_start, local_end)
                selected = Strip(
                    Segment.apply_style(selected, post_style=selection_style),
                    selected.cell_length,
                )
                line = Strip.join(
                    (
                        line.crop(0, local_start),
                        selected,
                        line.crop(local_end),
                    )
                )
        return line.apply_offsets(scroll_x, content_y)


class SpanDetailPanel(Vertical):
    """Replace the ledger with bounded, tabbed, scrollable span details."""

    can_focus = True
    COMPONENT_CLASSES: ClassVar[set[str]] = Widget.COMPONENT_CLASSES | {
        "span-detail--accent",
        "span-detail--code",
        "span-detail--error",
        "span-detail--muted",
        "span-detail--success",
        "span-detail--text",
    }

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
    SpanDetailPanel > #trajectory-span-detail-body {
        width: 1fr;
        height: 1fr;
        min-height: 0;
        layers: detail-content detail-loading;
    }
    SpanDetailPanel #trajectory-span-detail-tabs {
        layer: detail-content;
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
    SpanDetailPanel RichLog:focus {
        background-tint: transparent;
    }
    SpanDetailPanel > #trajectory-span-detail-body > LoadingIndicator {
        display: none;
        position: absolute;
        layer: detail-loading;
        width: 1fr;
        height: 1fr;
        color: $accent;
        background: $background;
    }
    SpanDetailPanel > .span-detail--accent {
        color: $accent;
        text-style: dim;
    }
    SpanDetailPanel > .span-detail--text {
        color: $text;
        background: $background;
    }
    SpanDetailPanel > .span-detail--code {
        color: $text;
        background: $background;
    }
    SpanDetailPanel > .span-detail--muted {
        color: $text-muted;
        background: $background;
        text-style: dim;
    }
    SpanDetailPanel > .span-detail--error {
        color: $error;
    }
    SpanDetailPanel > .span-detail--success {
        color: $success;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._record: TrajectoryRecord | None = None
        self._tool: TrajectoryToolOperation | None = None
        self._request: TrajectoryRequest | None = None
        self._details: SpanDetails | None = None
        self._collapsed_json_paths: set[str] = set()
        self._syncing_tabs = False
        self._rendered_widths: dict[InspectorTab, int] = {}
        self._reflow_pending = False
        self._reflow_force = False
        self._reflow_scroll_y: float | None = None

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
        with Vertical(id="trajectory-span-detail-body"):
            with TabbedContent(id="trajectory-span-detail-tabs"):
                for tab in InspectorTab:
                    with TabPane(tab.value.replace("_", " ").title(), id=self._pane_id(tab)):
                        yield _SelectableRichLog(
                            id=self._log_id(tab),
                            min_width=1,
                            wrap=True,
                            markup=False,
                            highlight=False,
                        )
            yield _DetailLoadingIndicator(id="trajectory-span-detail-loading")

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
        with span(REGIE_TRAJECTORY_DETAIL_PROJECT, tab=tab.value):
            styles = DetailStyles(
                text=self.get_component_rich_style("span-detail--text", partial=True),
                accent=self.get_component_rich_style("span-detail--accent", partial=True),
                code=self.get_component_rich_style("span-detail--code", partial=True),
                muted=self.get_component_rich_style("span-detail--muted", partial=True),
                error=self.get_component_rich_style("span-detail--error", partial=True),
                success=self.get_component_rich_style("span-detail--success", partial=True),
            )
            if self._tool is not None:
                return build_tool_span_details(
                    self._tool,
                    tab,
                    styles=styles,
                    collapsed_json_paths=frozenset(self._collapsed_json_paths),
                )
            if self._record is not None:
                return build_span_details(
                    self._record,
                    tab,
                    styles=styles,
                    request=self._request,
                    collapsed_json_paths=frozenset(self._collapsed_json_paths),
                )
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

    def _sync_chrome(self) -> None:
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

    def _write_content(self, width: int, scroll_y: float | None = None) -> None:
        if not self.is_mounted:
            return
        if self._details is None:
            return
        tab = self._details.tab
        log = self.query_one(f"#{self._log_id(self._details.tab)}", RichLog)
        log.clear()
        with span(REGIE_TRAJECTORY_DETAIL_RENDER, tab=tab.value):
            log.write(self._details.content, width=width, scroll_end=False)
        self._rendered_widths[tab] = width
        if scroll_y is None:
            log.scroll_home(animate=False)
        else:
            log.scroll_to(x=0, y=scroll_y, animate=False, force=True)

    def on_resize(self, _event: events.Resize) -> None:
        if self._details is None or not self.is_mounted:
            return
        log = self.query_one(f"#{self._log_id(self._details.tab)}", RichLog)
        self._schedule_reflow(float(log.scroll_y))

    def _schedule_reflow(
        self,
        scroll_y: float | None = None,
        *,
        force: bool = False,
        loading: bool = False,
    ) -> None:
        if not self.is_mounted or self._details is None:
            return
        if force or not self._reflow_pending:
            self._reflow_scroll_y = scroll_y
        self._reflow_force = self._reflow_force or force
        if loading:
            log = self.query_one(f"#{self._log_id(self._details.tab)}", RichLog)
            self._stop_loading()
            log.clear()
            self.query_one("#trajectory-span-detail-loading", _DetailLoadingIndicator).set_active(
                True
            )
        if self._reflow_pending:
            return
        self._reflow_pending = True
        if not self.call_after_refresh(self._reflow_content):
            self._reflow_pending = False
            self._reflow_force = False
            self._reflow_scroll_y = None
            self._stop_loading()

    def _stop_loading(self) -> None:
        if self.is_mounted:
            self.query_one("#trajectory-span-detail-loading", _DetailLoadingIndicator).set_active(
                False
            )

    def _reflow_content(self) -> None:
        self._reflow_pending = False
        if not self.is_mounted or self._details is None:
            self._reflow_force = False
            self._reflow_scroll_y = None
            self._stop_loading()
            return
        tab = self._details.tab
        log = self.query_one(f"#{self._log_id(self._details.tab)}", RichLog)
        width = log.scrollable_content_region.width
        if width <= 0:
            self._stop_loading()
            return
        force = self._reflow_force
        scroll_y = self._reflow_scroll_y
        self._reflow_force = False
        self._reflow_scroll_y = None
        try:
            if force or width != self._rendered_widths.get(tab):
                self._write_content(width, scroll_y)
        finally:
            self._stop_loading()

    def set_span(
        self,
        record: TrajectoryRecord,
        *,
        tool: TrajectoryToolOperation | None = None,
        request: TrajectoryRequest | None = None,
        tab: InspectorTab = InspectorTab.SUMMARY,
    ) -> InspectorTab:
        previous_tab = self._details.tab if self._details is not None else None
        if self._record is None or self._record.record_id != record.record_id:
            self._collapsed_json_paths.clear()
        if (
            self._record == record
            and self._tool == tool
            and self._request == request
            and self._details is not None
            and tab is self._details.tab
        ):
            if self.is_mounted:
                log = self.query_one(f"#{self._log_id(self._details.tab)}", RichLog)
                self._schedule_reflow(float(log.scroll_y))
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
        self._sync_chrome()
        keep_rendered_content = (
            preserve_scroll and self._details is not None and self._details.tab is previous_tab
        )
        # A live update can change the selected record's request or paired
        # tool while its detail is already on screen. Keep that content until
        # the next refresh writes its replacement; clearing it here exposes a
        # blank/loading frame on every incoming span. A different record or
        # effective tab still loads normally so stale detail is never shown as
        # current.
        self._schedule_reflow(
            scroll_y,
            force=True,
            loading=not keep_rendered_content,
        )
        return self.tab

    def set_tab(self, tab: InspectorTab) -> InspectorTab:
        if self._record is None or tab not in self.tabs or tab is self.tab:
            return self.tab
        self._details = self._build_details(tab)
        self._sync_chrome()
        self._schedule_reflow(force=True, loading=True)
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
        toggle_key = meta.get(DETAIL_JSON_TOGGLE_META)
        if isinstance(toggle_key, str):
            event.stop()
            self._toggle_json_path(toggle_key)
            return
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

    def _toggle_json_path(self, toggle_key: str) -> None:
        if self._details is None or not self.is_mounted:
            return
        log = self.query_one(f"#{self._log_id(self._details.tab)}", RichLog)
        scroll_y = float(log.scroll_y)
        if toggle_key in self._collapsed_json_paths:
            self._collapsed_json_paths.remove(toggle_key)
        else:
            self._collapsed_json_paths.add(toggle_key)
        self._details = self._build_details(self._details.tab)
        self._sync_chrome()
        self._schedule_reflow(scroll_y, force=True, loading=True)


__all__ = [
    "SpanDetailClosed",
    "SpanDetailPanel",
    "SpanDetailParticipantLinkClicked",
    "SpanDetailRecordLinkClicked",
    "SpanDetailTabChanged",
]
