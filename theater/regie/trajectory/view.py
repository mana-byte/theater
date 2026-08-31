"""Standalone trajectory surface and contextual focus-region actions."""

from __future__ import annotations

import contextlib
import inspect
from bisect import bisect_left, bisect_right
from collections.abc import Callable

from textual import events
from textual.app import ComposeResult
from textual.await_remove import AwaitRemove
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Input, Select
from textual.worker import Worker, WorkerCancelled, WorkerFailed

from theater.constants.regie_trajectory import (
    MAX_QUERY_BYTES,
    TIMELINE_HEIGHT,
    TRAJECTORY_HORIZONTAL_PADDING,
)
from theater.regie.trajectory.controller import TrajectoryController
from theater.regie.trajectory.enums import (
    INSIGHT_VIEWS,
    DiagnosticView,
    FilterDimension,
    FocusRegion,
    OrderMode,
)
from theater.regie.trajectory.inspection.project import active_detail_tab, detail_text
from theater.regie.trajectory.messages import (
    CopyRequest,
    ParticipantLinkRequest,
    ReturnToTree,
    TrajectoryBackRequested,
    TrajectoryCopyRequested,
    TrajectoryParticipantSelected,
    TrajectoryRetryRequested,
)
from theater.regie.trajectory.projection import TrajectoryViewProjection
from theater.regie.trajectory.render.diagnostics import is_raw_theater_bus_record
from theater.regie.trajectory.render.records import sanitize_text
from theater.regie.trajectory.search import SearchResult
from theater.regie.trajectory.state import ParticipantTrajectoryState, TrajectoryStateStore
from theater.regie.trajectory.widgets.breadcrumb import TrajectoryBreadcrumb
from theater.regie.trajectory.widgets.filter_panel import (
    FilterClearRequested,
    FilterPanel,
    FilterPanelClosed,
    FilterValueClicked,
)
from theater.regie.trajectory.widgets.footer import (
    FooterActionRequested,
    FooterPageRequested,
    FooterViewRequested,
    TrajectoryFooter,
)
from theater.regie.trajectory.widgets.header import TrajectoryHeader
from theater.regie.trajectory.widgets.hover_card import TimelineHoverCard
from theater.regie.trajectory.widgets.insights import (
    InsightActivated,
    InsightHighlighted,
    InsightsPanel,
)
from theater.regie.trajectory.widgets.ledger import (
    Ledger,
    LedgerOlderClicked,
    LedgerRecordClicked,
    LedgerRecordHovered,
    LedgerRetryClicked,
)
from theater.regie.trajectory.widgets.overview import TrajectoryOverviewStrip
from theater.regie.trajectory.widgets.search import TrajectorySearchInput
from theater.regie.trajectory.widgets.span_detail import (
    SpanDetailClosed,
    SpanDetailPanel,
    SpanDetailParticipantLinkClicked,
    SpanDetailRecordLinkClicked,
    SpanDetailTabChanged,
)
from theater.regie.trajectory.widgets.timeline import (
    Timeline,
    TimelineScrolled,
    TimelineSpanClicked,
    TimelineSpanHovered,
)
from theater.trajectory import (
    ParticipantLink,
    Timing,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryPage,
    TrajectoryRecord,
    TrajectoryRequest,
    TrajectoryStatus,
    TrajectoryToolOperation,
)
from theater.trajectory.location import TrajectoryLocationResolution


class TrajectoryView(Vertical):
    """A fixed timeline, viewport ledger, and filter chooser."""

    can_focus = True

    DEFAULT_CSS = f"""
    TrajectoryView {{
        width: 1fr;
        height: 1fr;
        min-width: 0;
        min-height: 0;
        layers: trajectory-base trajectory-overlay;
        background: $background;
        border-left: solid $foreground 20%;
        padding: 0 {TRAJECTORY_HORIZONTAL_PADDING};
    }}
    TrajectoryView > #trajectory-timeline,
    TrajectoryView > #trajectory-header,
    TrajectoryView > #trajectory-overview,
    TrajectoryView > #trajectory-ledger,
    TrajectoryView > #trajectory-insights,
    TrajectoryView > #trajectory-span-detail,
    TrajectoryView > #trajectory-footer {{
        layer: trajectory-base;
    }}
    TrajectoryView > #trajectory-timeline {{
        width: 1fr;
        height: {TIMELINE_HEIGHT};
        min-height: {TIMELINE_HEIGHT};
    }}
    TrajectoryView.-compact-height > #trajectory-overview {{
        display: none;
    }}
    TrajectoryView.-compact-height > #trajectory-timeline {{
        height: 6 !important;
        min-height: 6 !important;
    }}
    TrajectoryView.-short-height > #trajectory-timeline {{
        height: 4 !important;
        min-height: 4 !important;
    }}
    TrajectoryView > #trajectory-filters {{
        display: none;
    }}
    TrajectoryView > #trajectory-filters.-open {{
        display: block;
    }}
    TrajectoryView > #trajectory-ledger {{
        width: 1fr;
        height: 1fr;
        min-height: 0;
    }}
    TrajectoryView > #trajectory-insights {{
        width: 1fr;
        height: 1fr;
        min-height: 0;
    }}
    TrajectoryView > #trajectory-span-detail {{
        width: 1fr;
        height: 1fr;
        min-height: 0;
    }}
    TrajectoryView .-hidden {{
        display: none;
    }}
    """

    def __init__(
        self,
        participant_id: str,
        *,
        controller: TrajectoryController | None = None,
        state_store: TrajectoryStateStore | None = None,
        copy_request: CopyRequest | None = None,
        participant_link: ParticipantLinkRequest | None = None,
        focus_on_mount: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.participant_id = participant_id
        self.controller = controller
        if state_store is not None:
            self.state_store = state_store
        elif controller is not None:
            self.state_store = controller.state_store
        else:
            self.state_store = TrajectoryStateStore()
        self.state = (
            controller.state_for(participant_id)
            if controller is not None
            else self.state_store.get(participant_id)
        )
        self._copy_request = copy_request
        self._participant_link = participant_link
        self._focus_on_mount = focus_on_mount
        self._unsubscribe: Callable[[], None] | None = None
        self.projection = TrajectoryViewProjection(self.state, self.state_store.page_size)
        self._search_refresh_pending = False
        self._load_worker: Worker[TrajectoryPage | None] | None = None
        self._retiring = False
        self._responsive_timeline_height: int | None = None

    def compose(self) -> ComposeResult:
        yield TrajectoryOverviewStrip(id="trajectory-overview")
        yield TrajectoryHeader(id="trajectory-header")
        yield Timeline(id="trajectory-timeline")
        yield FilterPanel(id="trajectory-filters", classes="-hidden")
        yield Ledger(id="trajectory-ledger")
        yield InsightsPanel(id="trajectory-insights", classes="-hidden")
        yield SpanDetailPanel(id="trajectory-span-detail", classes="-hidden")
        yield TrajectoryFooter(id="trajectory-footer")
        yield TimelineHoverCard(id="trajectory-hover-card")

    def on_mount(self) -> None:
        if self.controller is not None:
            self._unsubscribe = self.controller.subscribe(self._controller_state_changed)
            self._load_worker = self.run_worker(
                self.controller.open(self.participant_id),
                name=f"trajectory-open-{self.participant_id}",
                exclusive=True,
            )
        self.call_after_refresh(self._finish_mount)

    def _finish_mount(self) -> None:
        if self._retiring or not self.is_attached:
            return
        self._sync_responsive_height(self.size.height)
        self._refresh()
        self._sync_search_drawer(animate=False)
        if self.state.search_open:
            self._focus_search()
        elif self._focus_on_mount:
            self.focus_region(self.state.focus_region)

    def remove(self) -> AwaitRemove:
        self._retiring = True
        return super().remove()

    def on_unmount(self) -> None:
        self._retiring = True
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._load_worker is not None and not self._load_worker.is_finished:
            self._load_worker.cancel()

    def on_resize(self, event: events.Resize) -> None:
        self._sync_responsive_height(event.size.height)
        if not self.is_mounted:
            return
        card = self.query_one("#trajectory-hover-card", TimelineHoverCard)
        card.fit_to_viewport(self.content_region.width)
        if card.record_id is not None:
            self.call_after_refresh(self._show_timeline_hover, card.record_id)

    def _sync_responsive_height(self, height: int) -> None:
        compact = height < 30
        short = height < 22
        self.set_class(compact, "-compact-height")
        self.set_class(short, "-short-height")
        if not self.is_attached:
            return
        timeline_height = 4 if short else 6 if compact else TIMELINE_HEIGHT
        if timeline_height == self._responsive_timeline_height:
            return
        timeline = self.query_one("#trajectory-timeline", Timeline)
        timeline.styles.height = timeline_height
        timeline.styles.min_height = timeline_height
        self._responsive_timeline_height = timeline_height
        self.refresh(layout=True)

    def _controller_state_changed(self, state: ParticipantTrajectoryState) -> None:
        if state.participant_id != self.participant_id:
            return
        self.state = state
        if not self._retiring and self.is_attached:
            self._refresh()

    @staticmethod
    def _set_class(widget: Widget, class_name: str, enabled: bool) -> bool:
        if widget.has_class(class_name) == enabled:
            return False
        widget.set_class(enabled, class_name)
        return True

    def _refresh(self, *, recompute: bool = True) -> None:
        records = self.projection.refresh(
            self.state,
            self.state_store.page_size,
            recompute=recompute,
        )
        if self._retiring or not self.is_attached:
            return
        timeline = self.query_one("#trajectory-timeline", Timeline)
        overview_strip = self.query_one("#trajectory-overview", TrajectoryOverviewStrip)
        breadcrumb = self.query_one("#trajectory-breadcrumb", TrajectoryBreadcrumb)
        ledger = self.query_one("#trajectory-ledger", Ledger)
        insights = self.query_one("#trajectory-insights", InsightsPanel)
        insight_view = self.state.diagnostic_view in INSIGHT_VIEWS
        if insight_view:
            selected_id = insights.update_analysis(
                self.state.diagnostic_view,
                self.state.analysis_index,
                frozenset(self.projection.search_result.record_ids),
                selected_id=self.state.selected_id,
                follow_tail=self.state.follow_tail,
            )
            if self.state.follow_tail:
                self.state.select(selected_id)
        search = self.query_one("#trajectory-search", Input)
        if search.value != self.state.query and self.app.focused is not search:
            search.value = self.state.query
        overview_strip.update_state(
            panel=self.state.panel,
            capabilities=self.state.capabilities,
            overview=self.state.overview,
            loading=self.state.loading,
            stale_message=self.state.stale_message,
        )
        timeline_offset: int | None = self.state.timeline_scroll
        if (
            not self.state.follow_tail
            and timeline.span_ids
            and timeline.horizontal_offset == timeline_offset
        ):
            timeline_offset = None
        timeline.update_records(
            records,
            matched_ids=self.projection.search_result.matched_ids,
            hovered_id=self.state.hovered_id,
            selected_id=self.state.selected_id,
            duration_mode=self.state.order_mode is OrderMode.DURATION,
            scroll_offset=timeline_offset,
        )
        if self.state.follow_tail:
            timeline.scroll_to_tail(repaint=False)
        hover_card = self.query_one("#trajectory-hover-card", TimelineHoverCard)
        if hover_card.record_id is not None and hover_card.record_id == self.state.hovered_id:
            self._show_timeline_hover(hover_card.record_id)
        else:
            hover_card.hide()
        self.state.timeline_scroll = timeline.horizontal_offset
        detail_open = self.state.detail_id is not None
        if not insight_view and not detail_open:
            ledger.update_rows(
                records,
                self.projection.ledger_page.result,
                selected_id=self.state.selected_id,
                hovered_id=self.state.hovered_id,
                order_mode=self.state.order_mode,
                has_older=self.state.has_older and self.projection.ledger_page.index == 0,
                loading_older=self.state.loading_older and self.projection.ledger_page.index == 0,
                retry_message=self.state.retry_message if self.state.retry_kind else None,
                position_offset=self.projection.ledger_page.first_item - 1,
            )
        record, request, tool = self._selection_context()
        breadcrumb.update_context(record, request=request, tool=tool)
        self._sync_detail_panel()
        self._sync_filter_panel(update_options=True)
        if self.state.follow_tail and not insight_view and not detail_open:
            ledger.scroll_to_record(self.state.selected_id)
        self._update_status()

    def _update_status(self) -> None:
        status = self.state.panel.state.value
        message = self.state.panel.message or self.state.stale_message
        pieces = [status]
        if self.state.loading:
            pieces.append("loading")
        if message:
            pieces.append(message)
        if self.state.follow_tail:
            pieces.append("following")
        elif self.state.new_count:
            pieces.append(f"↓ {self.state.new_count} new events · G to follow")
        if self.state.loading_older:
            pieces.append("loading older")
        if self.state.retry_kind:
            pieces.append("retry available")
        if self.state.reload_required:
            pieces.append("loaded window bounded; reload marker")
        if self.state.truncated_by_bytes:
            pieces.append("page byte cap reached")
        if self.state.filters_open:
            pieces.append("filters open")
        footer_message = sanitize_text(" · ".join(pieces[1:]))
        footer_message = footer_message.replace("\r", " ").replace("\n", " · ")
        footer = self.query_one("#trajectory-footer", TrajectoryFooter)
        insight_view = self.state.diagnostic_view in INSIGHT_VIEWS
        insight_count = (
            self.query_one("#trajectory-insights", InsightsPanel).insight_count
            if insight_view
            else 0
        )
        visible_count = (
            insight_count if insight_view else len(self.projection.search_result.row_ids)
        )
        page_number = 1 if insight_view else self.projection.ledger_page.number
        page_count = 1 if insight_view else self.projection.ledger_page.count
        first_item = (
            int(visible_count > 0) if insight_view else self.projection.ledger_page.first_item
        )
        last_item = visible_count if insight_view else self.projection.ledger_page.last_item
        footer.update_state(
            status=status,
            message=footer_message,
            record_count=(
                len(self.state.tool_index.ordered)
                + sum(
                    record.record_id not in self.state.tool_index.by_record_id
                    for record in self.projection.ordered_records
                )
            ),
            visible_count=visible_count,
            page_number=page_number,
            page_count=page_count,
            first_item=first_item,
            last_item=last_item,
            active_filters=(
                len(self.state.lane_filters)
                + len(self.state.kind_filters)
                + len(self.state.status_filters)
                + len(self.state.source_filters)
            ),
            query=self.state.query,
            mode=self.state.order_mode,
            view=self.state.diagnostic_view,
            follow_tail=self.state.follow_tail,
            new_count=self.state.new_count,
        )

    @property
    def search_result(self) -> SearchResult:
        return self.projection.search_result

    @property
    def active_region(self) -> FocusRegion:
        return self.state.focus_region

    def _content_region(self) -> FocusRegion:
        return (
            FocusRegion.INSIGHTS
            if self.state.diagnostic_view in INSIGHT_VIEWS
            else FocusRegion.LEDGER
        )

    def focus_region(self, region: FocusRegion) -> FocusRegion:
        if region is FocusRegion.DETAIL and self.state.detail_id is None:
            region = self._content_region()
        elif (
            region in {FocusRegion.LEDGER, FocusRegion.INSIGHTS}
            and self.state.detail_id is not None
        ):
            region = FocusRegion.DETAIL
        elif region is FocusRegion.LEDGER and self.state.diagnostic_view in INSIGHT_VIEWS:
            region = FocusRegion.INSIGHTS
        elif region is FocusRegion.INSIGHTS and self.state.diagnostic_view not in INSIGHT_VIEWS:
            region = FocusRegion.LEDGER
        self.state.focus_region = region
        if self.is_mounted:
            if region is not FocusRegion.TIMELINE:
                self.query_one("#trajectory-hover-card", TimelineHoverCard).hide()
            widget = {
                FocusRegion.TIMELINE: self.query_one("#trajectory-timeline", Timeline),
                FocusRegion.LEDGER: self.query_one("#trajectory-ledger", Ledger),
                FocusRegion.INSIGHTS: self.query_one("#trajectory-insights", InsightsPanel),
                FocusRegion.DETAIL: self.query_one("#trajectory-span-detail", SpanDetailPanel),
            }[region]
            widget.focus()
        return region

    def enter_live_tail(self) -> None:
        """Enter this trajectory at its live tail without stale transient details."""
        self.state.resume_follow()
        self.state.hovered_id = None
        self.state.detail_id = None
        self.state.focus_region = self._content_region()
        self._refresh()

    def _sync_filter_panel(self, *, update_options: bool = False) -> None:
        if not self.is_mounted:
            return
        panel = self.query_one("#trajectory-filters", FilterPanel)
        if self.state.filters_open and update_options:
            panel.update_filters(
                self.projection.search_result.counts,
                lanes=self.state.lane_filters,
                kinds=self.state.kind_filters,
                statuses=self.state.status_filters,
                sources=self.state.source_filters,
            )
        self._set_class(panel, "-open", self.state.filters_open)
        self._set_class(panel, "-hidden", not self.state.filters_open)

    def _sync_selection(self, *, scroll: bool = True, sync_insights: bool = True) -> None:
        if not self.is_mounted:
            return
        record_id = self.state.selected_id
        self.query_one("#trajectory-timeline", Timeline).set_selected(record_id)
        self.query_one("#trajectory-ledger", Ledger).set_selected(record_id)
        if sync_insights:
            self.query_one("#trajectory-insights", InsightsPanel).set_selected(record_id)
        record, request, tool = self._selection_context()
        self.query_one("#trajectory-breadcrumb", TrajectoryBreadcrumb).update_context(
            record, request=request, tool=tool
        )
        if scroll:
            self._scroll_to_record(record_id)
        self._update_status()

    def _scroll_to_record(self, record_id: str | None) -> None:
        if record_id is None or not self.is_mounted:
            return
        if self.state.diagnostic_view not in INSIGHT_VIEWS:
            row_id = self.projection.logical_row_id(record_id)
            if row_id not in self.projection.visible_id_set:
                return
            self.query_one("#trajectory-ledger", Ledger).scroll_to_record(record_id)
        timeline = self.query_one("#trajectory-timeline", Timeline)
        self.state.timeline_scroll = timeline.scroll_span_into_view(record_id)

    def _request_for_record(self, record_id: str | None) -> TrajectoryRequest | None:
        if record_id is None:
            return None
        request_id = self.state.request_index.by_record_id.get(record_id)
        return self.state.request_index.by_id.get(request_id or "")

    def _selection_context(
        self,
    ) -> tuple[
        TrajectoryRecord | None,
        TrajectoryRequest | None,
        TrajectoryToolOperation | None,
    ]:
        record = self.state.selected_record
        if record is None:
            return None, None, None
        request = self._request_for_record(record.record_id)
        operation_id = self.state.tool_index.by_record_id.get(record.record_id)
        tool = self.state.tool_index.by_id.get(operation_id or "")
        return record, request, tool

    def _sync_detail_panel(self) -> None:
        ledger = self.query_one("#trajectory-ledger", Ledger)
        insights = self.query_one("#trajectory-insights", InsightsPanel)
        panel = self.query_one("#trajectory-span-detail", SpanDetailPanel)
        insight_view = self.state.diagnostic_view in INSIGHT_VIEWS
        record_id = self.state.row_anchor(self.state.detail_id)
        record = self.state.records.get(record_id or "")
        if record is None:
            self.state.detail_id = None
            self._set_class(ledger, "-hidden", insight_view)
            self._set_class(insights, "-hidden", not insight_view)
            self._set_class(panel, "-hidden", True)
            if self.state.focus_region is FocusRegion.DETAIL:
                self.state.focus_region = self._content_region()
            return
        assert record_id is not None
        operation_id = self.state.tool_index.by_record_id.get(record_id)
        tool = self.state.tool_index.by_id.get(operation_id) if operation_id is not None else None
        self.state.detail_id = record_id
        self._set_class(ledger, "-hidden", True)
        self._set_class(insights, "-hidden", True)
        self._set_class(panel, "-hidden", False)
        self.state.detail_tab = panel.set_span(
            record,
            tool=tool,
            request=self._request_for_record(record_id),
            tab=self.state.detail_tab,
        )

    def select_and_reveal_record(self, record_id: str) -> bool:
        """Select and open a loaded record without requesting more trajectory data."""
        if record_id not in self.state.records:
            return False
        anchor = self.state.row_anchor(record_id)
        if anchor is None:
            return False
        if self.projection.logical_row_id(record_id) not in self.projection.all_visible_indices:
            record = self.state.records[record_id]
            self.state.diagnostic_view = (
                DiagnosticView.COORDINATION
                if is_raw_theater_bus_record(record)
                else DiagnosticView.ALL
            )
            self.state.query = ""
            if self.is_mounted:
                self.query_one("#trajectory-search", Input).value = ""
            self.state.lane_filters.clear()
            self.state.kind_filters.clear()
            self.state.status_filters.clear()
            self.state.source_filters.clear()
        self.state.select(anchor)
        self._update_follow_for_selection(anchor)
        self.state.detail_id = anchor
        self.state.focus_region = FocusRegion.DETAIL
        self._refresh()
        self._sync_selection()
        if self.is_mounted:
            self.query_one("#trajectory-span-detail", SpanDetailPanel).focus()
        return True

    async def wait_until_loaded(self) -> None:
        """Wait for the initial bounded snapshot before an exact reveal."""
        worker = self._load_worker
        if worker is None or worker.is_finished:
            return
        with contextlib.suppress(WorkerCancelled, WorkerFailed):
            await worker.wait()

    def _selected_visible_ids(self) -> tuple[str, ...]:
        return self.projection.visible_ids

    def _update_follow_for_selection(self, record_id: str | None) -> None:
        insight_view = self.state.diagnostic_view in INSIGHT_VIEWS and self.is_mounted
        if insight_view:
            at_tail = self.query_one("#trajectory-insights", InsightsPanel).is_tail_record(
                record_id
            )
        else:
            visible_ids = self.projection.all_visible_ids
            tail_id = visible_ids[-1] if visible_ids else None
            row_id = self.projection.logical_row_id(record_id)
            at_tail = row_id is not None and row_id == tail_id
        if at_tail:
            self.state.follow_tail = True
            self.state.new_count = 0
        else:
            self.state.pause_follow()

    def _reveal_selection_page(self, record_id: str | None) -> bool:
        index = self.projection.all_visible_indices.get(
            self.projection.logical_row_id(record_id) or ""
        )
        if index is None:
            return False
        page = index // self.state_store.page_size
        if page == self.state.ledger_page:
            return False
        self.state.ledger_page = page
        return True

    def _select(self, delta: int) -> None:
        before = self.state.selected_id
        visible_ids = self._selected_visible_ids()
        boundary = visible_ids[0 if delta < 0 else -1] if visible_ids else None
        if before == boundary and self._change_page(delta):
            return
        if (
            delta < 0
            and before
            and visible_ids
            and before == visible_ids[0]
            and self.state.ledger_page == 0
            and self.state.has_older
            and self.controller is not None
            and not self.state.loading_older
        ):
            self.run_worker(
                self.controller.load_older(self.participant_id),
                name="trajectory-older",
                group="trajectory-older",
                exclusive=True,
            )
        if visible_ids:
            current = self.projection.visible_indices.get(before) if before is not None else None
            if current is None:
                order_index = self.projection.ordered_indices.get(before or "")
                if order_index is None:
                    target = 0 if delta > 0 else len(visible_ids) - 1
                elif delta > 0:
                    target = min(
                        len(visible_ids) - 1,
                        bisect_right(self.projection.visible_positions, order_index),
                    )
                else:
                    target = max(0, bisect_left(self.projection.visible_positions, order_index) - 1)
            else:
                target = max(0, min(len(visible_ids) - 1, current + delta))
            self.state.select(visible_ids[target])
            self._update_follow_for_selection(visible_ids[target])
        else:
            self.state.select(None)
        self._sync_selection()

    def action_select_next(self) -> None:
        self._select(1)

    def action_select_previous(self) -> None:
        self._select(-1)

    def action_timeline_previous(self) -> None:
        self.focus_region(FocusRegion.TIMELINE)
        timeline = self.query_one("#trajectory-timeline", Timeline)
        record_id = timeline.move_span(-1)
        self._update_follow_for_selection(record_id)
        self.state.select(record_id)
        if self._reveal_selection_page(record_id):
            self._refresh(recompute=False)
        else:
            self._sync_selection()

    def action_timeline_next(self) -> None:
        self.focus_region(FocusRegion.TIMELINE)
        timeline = self.query_one("#trajectory-timeline", Timeline)
        record_id = timeline.move_span(1)
        self._update_follow_for_selection(record_id)
        self.state.select(record_id)
        if self._reveal_selection_page(record_id):
            self._refresh(recompute=False)
        else:
            self._sync_selection()

    def _change_page(self, delta: int) -> bool:
        if self.state.diagnostic_view in INSIGHT_VIEWS:
            return False
        target = max(
            0,
            min(self.projection.ledger_page.count - 1, self.state.ledger_page + delta),
        )
        if target == self.state.ledger_page:
            return False
        self._show_page(target, select_last=delta < 0)
        return True

    def _show_page(self, target: int, *, select_last: bool = False) -> None:
        page = self.projection.page_for(target, self.state_store.page_size)
        self.state.ledger_page = page.index
        if not page.record_ids:
            record_id = None
        elif select_last:
            record_id = page.record_ids[-1]
        else:
            record_id = page.record_ids[0]
        self.state.select(record_id)
        self._update_follow_for_selection(record_id)
        self._refresh(recompute=False)

    def action_previous_page(self) -> None:
        self._change_page(-1)

    def action_next_page(self) -> None:
        self._change_page(1)

    def action_select_page(self, page_index: int) -> None:
        target = max(0, min(self.projection.ledger_page.count - 1, page_index))
        if target != self.state.ledger_page:
            self._show_page(target)

    def action_toggle_mode(self) -> None:
        self.state.order_mode = (
            OrderMode.DURATION if self.state.order_mode is OrderMode.ORDER else OrderMode.ORDER
        )
        self._refresh(recompute=False)

    def action_cycle_diagnostic_view(self) -> None:
        views = tuple(DiagnosticView)
        current = views.index(self.state.diagnostic_view)
        self.action_set_diagnostic_view(views[(current + 1) % len(views)])

    def action_set_diagnostic_view(self, view: DiagnosticView) -> None:
        if view is self.state.diagnostic_view:
            return
        self.state.diagnostic_view = view
        self.state.ledger_page = 0
        if self.state.detail_id is None and self.state.focus_region is not FocusRegion.TIMELINE:
            self.state.focus_region = self._content_region()
        self._refresh()
        if self.is_mounted and self.state.detail_id is None:
            self.focus_region(self.state.focus_region)

    def action_open_search(self) -> None:
        was_open = self.state.search_open
        self.state.search_open = True
        self.state.filters_open = False
        if self.is_mounted:
            search = self.query_one("#trajectory-search", Input)
            search.value = self.state.query
            self._sync_search_drawer(animate=not was_open)
        self._sync_filter_panel()
        self._update_status()
        if self.is_mounted:
            self._focus_search()

    def _focus_search(self) -> None:
        if self.state.search_open and self.is_mounted:
            self.app.set_focus(self.query_one("#trajectory-search", Input), scroll_visible=False)

    def _sync_search_drawer(self, *, animate: bool) -> None:
        if not self.is_mounted:
            return
        search = self.query_one("#trajectory-search", TrajectorySearchInput)
        if self.state.search_open:
            search.reveal(animate=animate)
        else:
            search.conceal(animate=animate)

    def _close_search(self, *, restore_focus: bool = True, animate: bool = True) -> None:
        was_open = self.state.search_open
        self.state.search_open = False
        if was_open:
            self._sync_search_drawer(animate=animate)
        if restore_focus:
            self.focus_region(self.state.focus_region)

    def action_toggle_filters(self) -> None:
        self.state.filters_open = not self.state.filters_open
        if self.state.filters_open and self.state.search_open:
            self._close_search(restore_focus=False)
        self._sync_filter_panel(update_options=True)
        self._update_status()
        if self.state.filters_open and self.is_mounted:
            self.query_one("#trajectory-filters", FilterPanel).focus_options()

    def action_reset(self) -> None:
        self.state.reset_ui()
        if self.is_mounted:
            search = self.query_one("#trajectory-search", Input)
            search.value = ""
            self._sync_search_drawer(animate=True)
        self._refresh()
        self.focus_region(self._content_region())
        if self.controller is not None:
            self.run_worker(
                self.controller.resume_follow(self.participant_id),
                name="trajectory-follow",
            )

    def action_oldest(self) -> None:
        self.state.pause_follow()
        visible = self.projection.all_visible_ids
        self.state.ledger_page = 0
        self.state.select(visible[0] if visible else None)
        self.state.timeline_scroll = 0
        self._refresh(recompute=False)

    def action_tail(self) -> None:
        self.state.resume_follow()
        visible = self.projection.all_visible_ids
        self.state.select(visible[-1] if visible else None)
        self._refresh(recompute=False)
        if self.controller is not None:
            self.run_worker(
                self.controller.resume_follow(self.participant_id), name="trajectory-follow"
            )

    def _open_details(self, record_id: str | None) -> None:
        record_id = self.state.row_anchor(record_id)
        if record_id is None:
            return
        self.state.select(record_id)
        self.state.detail_id = record_id
        self.state.focus_region = FocusRegion.DETAIL
        self._sync_selection(scroll=False)
        if not self.is_mounted:
            return
        self._sync_detail_panel()
        self.state.timeline_scroll = self.query_one(
            "#trajectory-timeline", Timeline
        ).scroll_span_into_view(record_id)
        self.query_one("#trajectory-span-detail", SpanDetailPanel).focus()

    def _close_details(self) -> None:
        record_id = self.state.detail_id
        self.state.detail_id = None
        self.state.focus_region = self._content_region()
        if not self.is_mounted:
            return
        # The ledger is intentionally left untouched while hidden. Catch it up
        # once when returning from the detail panel instead of rebuilding it for
        # every live update behind the panel.
        self._refresh(recompute=False)
        if self.state.focus_region is FocusRegion.INSIGHTS:
            insights = self.query_one("#trajectory-insights", InsightsPanel)
            insights.set_selected(record_id)
            insights.focus()
        else:
            ledger = self.query_one("#trajectory-ledger", Ledger)
            ledger.scroll_to_record(record_id)
            ledger.focus()

    def action_open_details(self) -> None:
        if self.state.focus_region is FocusRegion.INSIGHTS:
            self.query_one("#trajectory-insights", InsightsPanel).activate_current()
            return
        if self.state.detail_id is None:
            self._open_details(self.state.selected_id)

    def action_cycle_region(self, delta: int = 1) -> None:
        regions = (
            (FocusRegion.TIMELINE, FocusRegion.DETAIL)
            if self.state.detail_id is not None
            else (FocusRegion.TIMELINE, self._content_region())
        )
        index = regions.index(self.state.focus_region)
        self.focus_region(regions[(index + delta) % len(regions)])

    async def _retry(self) -> None:
        if self.controller is not None:
            await self.controller.retry(self.participant_id)
        else:
            self.post_message(TrajectoryRetryRequested(self.participant_id))

    async def _copy(self, text: str) -> None:
        if self._copy_request is not None:
            result = self._copy_request(text)
            if inspect.isawaitable(result):
                await result
        else:
            self.post_message(TrajectoryCopyRequested(text))

    async def _call_participant_link(self, participant_id: str) -> None:
        if self._participant_link is not None:
            result = self._participant_link(participant_id)
            if inspect.isawaitable(result):
                await result

    def action_copy(self) -> None:
        if self.state.detail_id is not None:
            text = self.query_one("#trajectory-span-detail", SpanDetailPanel).copy_text
        elif self.state.selected_record is not None:
            record = self.state.selected_record
            request = self._request_for_record(record.record_id)
            text = detail_text(
                record,
                active_detail_tab(record, self.state.detail_tab, request),
                request,
            )
        else:
            text = ""
        self.run_worker(self._copy(text), name="trajectory-copy")

    def action_retry(self) -> None:
        self.run_worker(self._retry(), name="trajectory-retry")

    def action_back(self) -> None:
        self.post_message(TrajectoryBackRequested())

    def action_return_to_tree(self) -> None:
        self.post_message(ReturnToTree())

    def _handle_contextual_horizontal(self, delta: int) -> None:
        if self.state.focus_region is FocusRegion.TIMELINE:
            if delta < 0:
                self.action_timeline_previous()
            else:
                self.action_timeline_next()
        else:
            panel = self.query_one("#trajectory-span-detail", SpanDetailPanel)
            if self.state.detail_id is not None:
                self.state.detail_tab = panel.move_tab(delta)

    def _handle_contextual_vertical(self, delta: int) -> None:
        if self.state.focus_region is FocusRegion.DETAIL:
            self.query_one("#trajectory-span-detail", SpanDetailPanel).scroll_content(delta)
        elif self.state.focus_region is FocusRegion.INSIGHTS:
            record_id = self.query_one("#trajectory-insights", InsightsPanel).move_row(delta)
            if record_id is not None:
                self._update_follow_for_selection(record_id)
                self.state.select(record_id)
                self._sync_selection(sync_insights=False)
        elif delta < 0:
            self.action_select_previous()
        else:
            self.action_select_next()

    def _handle_escape(self) -> None:
        if self.state.detail_id is not None:
            self._close_details()
        else:
            self.action_return_to_tree()

    def _search_owns_focus(self) -> bool:
        return self.is_mounted and self.app.focused is self.query_one("#trajectory-search", Input)

    def _filters_own_focus(self) -> bool:
        return (
            self.is_mounted and self.query_one("#trajectory-filters", FilterPanel).has_focus_within
        )

    def _page_select_owns_focus(self) -> bool:
        return self.is_mounted and any(
            self.query_one(selector, Select).has_focus_within
            for selector in ("#trajectory-page", "#trajectory-view-action")
        )

    def on_key(self, event: events.Key) -> None:
        if self.state.search_open:
            if event.key == "escape":
                event.stop()
                self._close_search()
            elif self.is_mounted and event.is_printable and event.character is not None:
                event.stop()
                self.query_one("#trajectory-search", Input).insert_text_at_cursor(event.character)
            return
        if self._filters_own_focus():
            if event.key == "escape":
                event.stop()
                self.on_filter_panel_closed(FilterPanelClosed())
            return
        if self._page_select_owns_focus():
            return
        actions: dict[str, Callable[[], None]] = {
            "j": lambda: self._handle_contextual_vertical(1),
            "down": lambda: self._handle_contextual_vertical(1),
            "k": lambda: self._handle_contextual_vertical(-1),
            "up": lambda: self._handle_contextual_vertical(-1),
            "h": lambda: self._handle_contextual_horizontal(-1),
            "left": lambda: self._handle_contextual_horizontal(-1),
            "l": lambda: self._handle_contextual_horizontal(1),
            "right": lambda: self._handle_contextual_horizontal(1),
            "g": self.action_oldest,
            "G": self.action_tail,
            "shift+g": self.action_tail,
            "H": self.action_previous_page,
            "shift+h": self.action_previous_page,
            "L": self.action_next_page,
            "shift+l": self.action_next_page,
            "/": self.action_open_search,
            "slash": self.action_open_search,
            "f": self.action_toggle_filters,
            "d": self.action_toggle_mode,
            "v": self.action_cycle_diagnostic_view,
            "b": self.action_back,
            "r": self.action_reset,
            "R": self.action_retry,
            "shift+r": self.action_retry,
            "y": self.action_copy,
            "enter": self.action_open_details,
            "tab": lambda: self.action_cycle_region(1),
            "shift+tab": lambda: self.action_cycle_region(-1),
            "escape": self._handle_escape,
        }
        action = actions.get(event.key)
        if action is None:
            return
        event.stop()
        action()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "trajectory-search":
            return
        self.state.query = event.value.encode("utf-8")[:MAX_QUERY_BYTES].decode(
            "utf-8", errors="ignore"
        )
        if not self._search_refresh_pending:
            self._search_refresh_pending = self.call_after_refresh(self._refresh_search)

    def _refresh_search(self) -> None:
        self._search_refresh_pending = False
        self._refresh()

    def on_input_blurred(self, event: Input.Blurred) -> None:
        if event.input.id == "trajectory-search":
            self._close_search(restore_focus=False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "trajectory-search":
            return
        event.stop()
        self._close_search()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        widget = event.widget
        timeline = self.query_one("#trajectory-timeline", Timeline)
        ancestors = set(widget.ancestors)
        if widget is not timeline and timeline not in ancestors:
            self.query_one("#trajectory-hover-card", TimelineHoverCard).hide()
        search = self.query_one("#trajectory-search", Input)
        if widget is search:
            was_open = self.state.search_open
            self.state.search_open = True
            if not was_open:
                self._sync_search_drawer(animate=True)
            return
        regions = (
            (FocusRegion.TIMELINE, timeline),
            (FocusRegion.LEDGER, self.query_one("#trajectory-ledger", Ledger)),
            (FocusRegion.INSIGHTS, self.query_one("#trajectory-insights", InsightsPanel)),
            (
                FocusRegion.DETAIL,
                self.query_one("#trajectory-span-detail", SpanDetailPanel),
            ),
        )
        for region, container in regions:
            if widget is container or container in ancestors:
                self.state.focus_region = region
                break

    def on_timeline_span_hovered(self, message: TimelineSpanHovered) -> None:
        self.state.hovered_id = message.record_id
        if self.is_mounted:
            self.query_one("#trajectory-timeline", Timeline).set_hovered(message.record_id)
            self.query_one("#trajectory-ledger", Ledger).set_hovered(message.record_id)
            self._show_timeline_hover(message.record_id)

    def _show_timeline_hover(self, record_id: str | None) -> None:
        card = self.query_one("#trajectory-hover-card", TimelineHoverCard)
        timeline = self.query_one("#trajectory-timeline", Timeline)
        card.fit_to_viewport(self.content_region.width)
        record = self.state.records.get(record_id or "")
        anchor = timeline.hover_anchor(record_id)
        if record is None or anchor is None:
            card.hide()
            return
        timing, scope = self._hover_timing(record.record_id)
        card.show_record(record, anchor, timing=timing, timing_scope=scope)

    def _hover_timing(self, record_id: str) -> tuple[Timing | None, str | None]:
        operation_id = self.state.tool_index.by_record_id.get(record_id)
        if operation_id is not None:
            timing = self.state.tool_index.by_id[operation_id].timing
            if timing is not None:
                return timing, "tool"
        request_id = self.state.request_index.by_record_id.get(record_id)
        if request_id is not None:
            timing = self.state.request_index.by_id[request_id].timing
            if timing is not None:
                return timing, "request"
        return self.state.records[record_id].timing, None

    def on_timeline_span_clicked(self, message: TimelineSpanClicked) -> None:
        self._update_follow_for_selection(message.record_id)
        self.state.select(message.record_id)
        self.state.focus_region = FocusRegion.TIMELINE
        detail_open = self.state.detail_id is not None
        if detail_open:
            self.state.detail_id = self.state.row_anchor(message.record_id)
        if self._reveal_selection_page(message.record_id):
            self._refresh(recompute=False)
        else:
            self._sync_selection()
            if detail_open:
                self._sync_detail_panel()

    def on_timeline_scrolled(self, message: TimelineScrolled) -> None:
        self.state.timeline_scroll = message.offset
        timeline = self.query_one("#trajectory-timeline", Timeline)
        card = self.query_one("#trajectory-hover-card", TimelineHoverCard)
        if card.record_id is not None:
            self._show_timeline_hover(card.record_id)
        if message.offset < timeline.tail_offset:
            self.state.pause_follow()
            self._update_status()

    def on_ledger_record_hovered(self, message: LedgerRecordHovered) -> None:
        self.state.hovered_id = message.record_id
        if self.is_mounted:
            self.query_one("#trajectory-hover-card", TimelineHoverCard).hide()
            self.query_one("#trajectory-timeline", Timeline).set_hovered(message.record_id)
            self.query_one("#trajectory-ledger", Ledger).set_hovered(message.record_id)

    def on_ledger_record_clicked(self, message: LedgerRecordClicked) -> None:
        self._update_follow_for_selection(message.record_id)
        self.state.select(message.record_id)
        self._open_details(message.record_id)

    def on_insight_highlighted(self, message: InsightHighlighted) -> None:
        self.state.hovered_id = message.record_id
        if not self.is_mounted:
            return
        self.query_one("#trajectory-hover-card", TimelineHoverCard).hide()
        self.query_one("#trajectory-timeline", Timeline).set_hovered(message.record_id)
        self.query_one("#trajectory-ledger", Ledger).set_hovered(message.record_id)

    def on_insight_activated(self, message: InsightActivated) -> None:
        if message.link is not None:
            self._activate_participant_link(
                message.link,
                exact=message.link.target_record_id is not None,
            )
            return
        self._update_follow_for_selection(message.record_id)
        self.state.select(message.record_id)
        self._open_details(message.record_id)

    def on_ledger_retry_clicked(self, _message: LedgerRetryClicked) -> None:
        self.action_retry()

    def on_ledger_older_clicked(self, _message: LedgerOlderClicked) -> None:
        if self.controller is not None and self.state.has_older and not self.state.loading_older:
            self.run_worker(
                self.controller.load_older(self.participant_id),
                name="trajectory-older",
                group="trajectory-older",
                exclusive=True,
            )

    def on_filter_value_clicked(self, message: FilterValueClicked) -> None:
        if message.dimension is FilterDimension.LANE:
            lane = TrajectoryLane(message.value)
            if lane in self.state.lane_filters:
                self.state.lane_filters.remove(lane)
            else:
                self.state.lane_filters.add(lane)
        elif message.dimension is FilterDimension.KIND:
            kind = TrajectoryKind(message.value)
            if kind in self.state.kind_filters:
                self.state.kind_filters.remove(kind)
            else:
                self.state.kind_filters.add(kind)
        elif message.dimension is FilterDimension.STATUS:
            status = TrajectoryStatus(message.value)
            if status in self.state.status_filters:
                self.state.status_filters.remove(status)
            else:
                self.state.status_filters.add(status)
        else:
            source = message.value
            if source in self.state.source_filters:
                self.state.source_filters.remove(source)
            else:
                self.state.source_filters.add(source)
        self._refresh()

    def on_filter_panel_closed(self, _message: FilterPanelClosed) -> None:
        self.state.filters_open = False
        self._sync_filter_panel()
        self._update_status()
        self.focus_region(self.state.focus_region)

    def on_filter_clear_requested(self, _message: FilterClearRequested) -> None:
        self.state.lane_filters.clear()
        self.state.kind_filters.clear()
        self.state.status_filters.clear()
        self.state.source_filters.clear()
        self._refresh()
        self.query_one("#trajectory-filters", FilterPanel).focus_options()

    def on_footer_action_requested(self, message: FooterActionRequested) -> None:
        actions = {
            "previous_page": self.action_previous_page,
            "next_page": self.action_next_page,
            "search": self.action_open_search,
            "filters": self.action_toggle_filters,
            "mode": self.action_toggle_mode,
            "follow": self.action_tail,
        }
        action = actions.get(message.action)
        if action is not None:
            action()

    def on_footer_page_requested(self, message: FooterPageRequested) -> None:
        self.action_select_page(message.page_index)

    def on_footer_view_requested(self, message: FooterViewRequested) -> None:
        self.action_set_diagnostic_view(message.view)

    def on_span_detail_participant_link_clicked(
        self, message: SpanDetailParticipantLinkClicked
    ) -> None:
        self._activate_participant_link(
            message.link,
            exact=message.exact,
            unresolved=message.unresolved,
        )

    def _activate_participant_link(
        self,
        link: ParticipantLink,
        *,
        exact: bool | None = None,
        unresolved: bool = False,
    ) -> None:
        if link.target_record_id is None and self._participant_link is not None:
            self.run_worker(
                self._call_participant_link(link.participant_id),
                name="trajectory-participant-link",
            )
        else:
            self.post_message(
                TrajectoryParticipantSelected(
                    link.participant_id,
                    link.target_record_id,
                    exact=exact,
                    unresolved=unresolved,
                    link=link,
                )
            )

    def on_span_detail_record_link_clicked(self, message: SpanDetailRecordLinkClicked) -> None:
        self.run_worker(
            self._reveal_record_link(message.record_id),
            name="trajectory-record-link",
            group="trajectory-record-link",
            exclusive=True,
        )

    async def _reveal_record_link(self, record_id: str) -> None:
        if self.select_and_reveal_record(record_id):
            return
        if self.controller is None:
            self.notify("linked event is outside the loaded window", severity="warning")
            return
        try:
            location = await self.controller.locate(self.participant_id, record_id)
        except Exception as exc:
            self.notify(f"linked event lookup failed: {exc}", severity="warning")
            return
        if location.resolution is not TrajectoryLocationResolution.EXACT or location.record is None:
            self.notify(location.message or "linked event is unavailable", severity="warning")
            return
        self.state.upsert((location.record,))
        if not self.select_and_reveal_record(record_id):
            self.notify("linked event could not be shown", severity="warning")

    def on_span_detail_tab_changed(self, message: SpanDetailTabChanged) -> None:
        self.state.detail_tab = message.tab

    def on_span_detail_closed(self, _message: SpanDetailClosed) -> None:
        self._close_details()


__all__ = [
    "ReturnToTree",
    "TrajectoryBackRequested",
    "TrajectoryCopyRequested",
    "TrajectoryParticipantSelected",
    "TrajectoryRetryRequested",
    "TrajectoryView",
]
