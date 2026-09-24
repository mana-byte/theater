"""Keyboard-first trajectory: a lane timeline above the selected span's details."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Callable

from textual import events
from textual.app import ComposeResult
from textual.await_remove import AwaitRemove
from textual.containers import Vertical
from textual.timer import Timer
from textual.widgets import Input
from textual.worker import Worker, WorkerCancelled, WorkerFailed

from regie.trajectory.domain import ParticipantLink, Timing, TrajectoryPage, TrajectoryRequest
from regie.trajectory.domain.location import TrajectoryLocationResolution
from regie.trajectory.rich.controller import TrajectoryController
from regie.trajectory.rich.enums import FocusRegion
from regie.trajectory.rich.messages import (
    CopyRequest,
    ParticipantLinkRequest,
    ReturnToTree,
    TrajectoryBackRequested,
    TrajectoryCopyRequested,
    TrajectoryParticipantSelected,
    TrajectoryRetryRequested,
)
from regie.trajectory.rich.projection import TrajectoryViewProjection
from regie.trajectory.rich.render.timeline import POINT_EVENT_KINDS
from regie.trajectory.rich.state import ParticipantTrajectoryState, TrajectoryStateStore
from regie.trajectory.rich.widgets.footer import TrajectoryFooter
from regie.trajectory.rich.widgets.header import TrajectoryHeader
from regie.trajectory.rich.widgets.search import TrajectorySearchInput
from regie.trajectory.rich.widgets.span_detail import (
    SpanDetailCopyRequested,
    SpanDetailPanel,
    SpanDetailParticipantLinkClicked,
    SpanDetailRecordLinkClicked,
)
from regie.trajectory.rich.widgets.timeline import (
    Timeline,
    TimelineScrolled,
    TimelineSpanClicked,
)
from regie.trajectory.ui_constants import (
    MAX_QUERY_BYTES,
    TIMELINE_ZOOM_MAX,
    TIMELINE_ZOOM_MIN,
    TIMELINE_ZOOM_STEP,
    TRAJECTORY_DETAIL_SETTLE_SECONDS,
    TRAJECTORY_DETAIL_SYNC_SECONDS,
    TRAJECTORY_HEADER_HEIGHT,
    TRAJECTORY_SEARCH_DEBOUNCE_SECONDS,
)


class TrajectoryView(Vertical):
    """Navigate spans on the timeline; Enter reads the selected span's details."""

    can_focus = True

    DEFAULT_CSS = f"""
    TrajectoryView {{
        width: 1fr;
        height: 1fr;
        min-width: 0;
        min-height: 0;
        background: $background;
        border-left: solid $foreground 20%;
    }}
    TrajectoryView > #trajectory-top {{
        width: 1fr;
        height: {TRAJECTORY_HEADER_HEIGHT};
        layers: trajectory-header trajectory-search;
    }}
    TrajectoryView > #trajectory-top > TrajectoryHeader {{
        layer: trajectory-header;
    }}
    TrajectoryView.-timeline-focus > #trajectory-top > TrajectoryHeader {{
        background: $accent 18%;
    }}
    TrajectoryView > #trajectory-top > TrajectorySearchInput {{
        dock: top;
        layer: trajectory-search;
    }}
    TrajectoryView > #trajectory-span-detail {{
        height: 1fr;
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
        self.projection = TrajectoryViewProjection()
        self._search_refresh_pending = False
        self._load_worker: Worker[TrajectoryPage | None] | None = None
        self._search_worker: Worker[None] | None = None
        self._retiring = False
        self._detail_timer: Timer | None = None
        self._detail_settling = False

    def compose(self) -> ComposeResult:
        with Vertical(id="trajectory-top"):
            yield TrajectoryHeader(id="trajectory-header")
            yield TrajectorySearchInput(placeholder="⌕  Search spans", id="trajectory-search")
        yield Timeline(id="trajectory-timeline")
        yield SpanDetailPanel(id="trajectory-span-detail")
        yield TrajectoryFooter(id="trajectory-footer")

    # ---- lifecycle ----------------------------------------------------------

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
        self._refresh()
        if self.state.search_open:
            self.action_open_search(animate=False)
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
        for worker in (self._load_worker, self._search_worker):
            if worker is not None and not worker.is_finished:
                worker.cancel()

    async def wait_until_loaded(self) -> None:
        """Wait for the initial bounded snapshot before an exact reveal."""
        worker = self._load_worker
        if worker is not None and not worker.is_finished:
            with contextlib.suppress(WorkerCancelled, WorkerFailed):
                await worker.wait()

    def _controller_state_changed(self, state: ParticipantTrajectoryState) -> None:
        if state.participant_id != self.participant_id:
            return
        self.state = state
        if not self._retiring and self.is_attached:
            self._refresh()

    # ---- rendering ----------------------------------------------------------

    def _refresh(self) -> None:
        records = self.projection.refresh(self.state)
        if self._retiring or not self.is_attached:
            return
        if self.state.selected_id not in self.projection.indices and records:
            self.state.select(records[-1].record_id)
        timeline = self.query_one("#trajectory-timeline", Timeline)
        timeline.set_zoom(self.state.timeline_zoom)
        timeline.update_records(
            records,
            matched_ids=self.projection.matched_ids,
            selected_id=self.state.selected_id,
            scroll_offset=None if self.state.follow_tail else self.state.timeline_scroll,
            timing_for=self._timing_for,
        )
        if self.state.follow_tail:
            timeline.scroll_to_tail(repaint=False)
        self.state.timeline_scroll = timeline.horizontal_offset
        self.query_one("#trajectory-header", TrajectoryHeader).update_state(
            panel=self.state.panel,
            overview=self.state.overview,
            loading=self.state.loading,
            stale_message=self.state.stale_message,
        )
        self._schedule_detail_sync()
        self._update_footer()

    def _timing_for(self, record_id: str) -> Timing | None:
        """A tool operation's interval, else a request's, for split-timing records."""
        state = self.state
        operation = state.tool_index.by_id.get(state.tool_index.by_record_id.get(record_id, ""))
        if operation is not None and operation.timing is not None:
            return operation.timing
        record = state.record_for_id(record_id)
        if record is None or record.kind in POINT_EVENT_KINDS:
            return None
        request = state.request_index.by_id.get(state.request_index.by_record_id.get(record_id, ""))
        return request.timing if request is not None else None

    def _schedule_detail_sync(self, *, cursor_moved: bool = False) -> None:
        """Rebuild details once selection settles; a moving cursor shows a loading state."""
        if self._detail_timer is not None:
            self._detail_timer.stop()
        panel = self.query_one("#trajectory-span-detail", SpanDetailPanel)
        moved = cursor_moved and panel.record_id != self.state.row_anchor(self.state.selected_id)
        if moved:
            panel.show_pending()
        elif self._detail_timer is not None and self._detail_settling:
            moved = True  # a live update must not cut short a cursor that is still settling
        self._detail_settling = moved
        delay = TRAJECTORY_DETAIL_SETTLE_SECONDS if moved else TRAJECTORY_DETAIL_SYNC_SECONDS
        self._detail_timer = self.set_timer(delay, self._sync_detail)

    def _sync_detail(self) -> None:
        self._detail_timer = None
        self._detail_settling = False
        panel = self.query_one("#trajectory-span-detail", SpanDetailPanel)
        record_id = self.state.row_anchor(self.state.selected_id)
        record = self.state.record_for_id(record_id)
        if record is None or record_id is None:
            panel.hide_pending()
            return
        operation_id = self.state.tool_index.by_record_id.get(record_id)
        panel.set_span(
            record,
            tool=self.state.tool_index.by_id.get(operation_id or ""),
            request=self._request_for_record(record_id),
            lookup=self.state.record_for_id,
        )

    def _request_for_record(self, record_id: str) -> TrajectoryRequest | None:
        request_id = self.state.request_index.by_record_id.get(record_id)
        return self.state.request_index.by_id.get(request_id or "")

    def _update_footer(self) -> None:
        state = self.state
        pieces = []
        if state.loading or state.loading_older:
            pieces.append("loading…")
        if state.follow_tail:
            pieces.append("● live")
        elif state.new_count:
            pieces.append(f"↓ {state.new_count} new · L to follow")
        if state.searching_full_history:
            pieces.append("searching full history…")
        elif state.query.strip():
            pieces.append(f"{len(self.projection.matched_ids)} matches")
        if state.search_error:
            pieces.append(state.search_error)
        if state.retry_kind:
            pieces.append("R to retry")
        self.query_one("#trajectory-footer", TrajectoryFooter).update_state(
            detail_focused=state.focus_region is FocusRegion.DETAIL,
            status="  ·  ".join(pieces),
        )

    # ---- focus and selection -------------------------------------------------

    def _flush_detail(self) -> None:
        """Apply a pending detail rebuild now, before details are read or focused."""
        if self._detail_timer is not None:
            self._detail_timer.stop()
            self._sync_detail()

    def focus_region(self, region: FocusRegion) -> FocusRegion:
        if region is FocusRegion.DETAIL:
            self._flush_detail()
        self.state.focus_region = region
        if self.is_mounted and self.is_attached:
            selector = "#trajectory-span-detail" if region is FocusRegion.DETAIL else Timeline
            # Apply now so a queued focus cannot override a newer search action.
            self.screen.set_focus(self.query_one(selector))
            self._update_footer()
        return region

    def enter_live_tail(self) -> None:
        """Enter this trajectory at its live tail."""
        self.state.resume_follow()
        self.state.focus_region = FocusRegion.TIMELINE
        self._refresh()

    def _select(self, record_id: str | None) -> None:
        if record_id is None:
            return
        self.state.select(record_id)
        records = self.projection.records
        if records and record_id == records[-1].record_id:
            self.state.follow_tail = True
            self.state.new_count = 0
        else:
            self.state.pause_follow()
        timeline = self.query_one("#trajectory-timeline", Timeline)
        timeline.set_selected(record_id)
        self.state.timeline_scroll = timeline.scroll_span_into_view(record_id)
        self._schedule_detail_sync(cursor_moved=True)
        self._update_footer()

    def select_and_reveal_record(self, record_id: str) -> bool:
        """Select a loaded record and focus its details."""
        anchor = self.state.row_anchor(record_id)
        if anchor is None:
            return False
        if anchor not in self.projection.indices and self.state.query:
            self.state.query = ""
            self.state.begin_search("")
            self._refresh()
        self._select(anchor)
        self.focus_region(FocusRegion.DETAIL)
        return True

    # ---- actions ---------------------------------------------------------------

    def action_move_span(self, delta: int) -> None:
        timeline = self.query_one("#trajectory-timeline", Timeline)
        if delta < 0 and timeline.is_lane_start(timeline.selected_id):
            self._load_older()
        self._select(timeline.move_span(delta))

    def action_move_lane(self, delta: int) -> None:
        self._select(self.query_one("#trajectory-timeline", Timeline).move_lane(delta))

    def action_zoom(self, factor: float) -> None:
        zoom = min(TIMELINE_ZOOM_MAX, max(TIMELINE_ZOOM_MIN, self.state.timeline_zoom * factor))
        self.state.timeline_zoom = zoom
        timeline = self.query_one("#trajectory-timeline", Timeline)
        timeline.set_zoom(zoom)
        if self.state.follow_tail:
            timeline.scroll_to_tail()
        self.state.timeline_scroll = timeline.horizontal_offset

    def action_match(self, delta: int) -> None:
        self._select(self.projection.match(self.state.selected_id, delta))

    def action_oldest(self) -> None:
        if self.projection.records:
            self._select(self.projection.records[0].record_id)

    def action_tail(self) -> None:
        self.state.resume_follow()
        self._refresh()
        if self.controller is not None:
            self.run_worker(
                self.controller.resume_follow(self.participant_id), name="trajectory-follow"
            )

    def action_reset(self) -> None:
        self.state.reset_ui()
        self.query_one("#trajectory-search", Input).value = ""
        self._refresh()
        self.focus_region(FocusRegion.TIMELINE)

    def action_copy(self, *, page: bool = False) -> None:
        """Copy the selected detail section, or the whole page."""
        self._flush_detail()
        panel = self.query_one("#trajectory-span-detail", SpanDetailPanel)
        text = panel.page_copy_text if page else panel.copy_text
        self.run_worker(self._copy(text), name="trajectory-copy")

    def action_retry(self) -> None:
        if self.controller is not None:
            self.run_worker(self.controller.retry(self.participant_id), name="trajectory-retry")
        else:
            self.post_message(TrajectoryRetryRequested(self.participant_id))

    def _load_older(self) -> None:
        if self.controller is not None and self.state.has_older and not self.state.loading_older:
            self.run_worker(
                self.controller.load_older(self.participant_id),
                name="trajectory-older",
                group="trajectory-older",
                exclusive=True,
            )

    async def _copy(self, text: str) -> None:
        if self._copy_request is None:
            self.post_message(TrajectoryCopyRequested(text))
            return
        result = self._copy_request(text)
        if inspect.isawaitable(result):
            await result

    # ---- keys -------------------------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        if self._search_focused():
            if event.key == "escape":
                event.stop()
                self._close_search()
            return
        detail = self.state.focus_region is FocusRegion.DETAIL
        action = (self._detail_keys() if detail else self._timeline_keys()).get(event.key)
        if action is None:
            action = self._shared_keys().get(event.key)
        if action is not None:
            event.stop()
            action()

    def _timeline_keys(self) -> dict[str, Callable[[], object]]:
        return {
            "h": lambda: self.action_move_span(-1),
            "left": lambda: self.action_move_span(-1),
            "l": lambda: self.action_move_span(1),
            "right": lambda: self.action_move_span(1),
            "j": lambda: self.action_move_lane(1),
            "down": lambda: self.action_move_lane(1),
            "k": lambda: self.action_move_lane(-1),
            "up": lambda: self.action_move_lane(-1),
            "H": self.action_oldest,
            "shift+h": self.action_oldest,
            "L": self.action_tail,
            "shift+l": self.action_tail,
            "n": lambda: self.action_match(1),
            "N": lambda: self.action_match(-1),
            "shift+n": lambda: self.action_match(-1),
            "enter": lambda: self.focus_region(FocusRegion.DETAIL),
            "+": lambda: self.action_zoom(TIMELINE_ZOOM_STEP),
            "plus": lambda: self.action_zoom(TIMELINE_ZOOM_STEP),
            "=": lambda: self.action_zoom(TIMELINE_ZOOM_STEP),
            "equals_sign": lambda: self.action_zoom(TIMELINE_ZOOM_STEP),
            "-": lambda: self.action_zoom(1 / TIMELINE_ZOOM_STEP),
            "minus": lambda: self.action_zoom(1 / TIMELINE_ZOOM_STEP),
            "r": self.action_reset,
            "escape": lambda: self.post_message(ReturnToTree()),
        }

    def _detail_keys(self) -> dict[str, Callable[[], object]]:
        panel = self.query_one("#trajectory-span-detail", SpanDetailPanel)

        return {
            "j": lambda: panel.scroll_content(1),
            "down": lambda: panel.scroll_content(1),
            "k": lambda: panel.scroll_content(-1),
            "up": lambda: panel.scroll_content(-1),
            "h": lambda: panel.move(-1),
            "left": lambda: panel.move(-1),
            "l": lambda: panel.move(1),
            "right": lambda: panel.move(1),
            "enter": panel.toggle,
            "Y": lambda: self.action_copy(page=True),
            "shift+y": lambda: self.action_copy(page=True),
            "escape": lambda: self.focus_region(FocusRegion.TIMELINE),
        }

    def _shared_keys(self) -> dict[str, Callable[[], object]]:
        return {
            "/": self.action_open_search,
            "slash": self.action_open_search,
            "y": self.action_copy,
            "b": lambda: self.post_message(TrajectoryBackRequested()),
            "R": self.action_retry,
            "shift+r": self.action_retry,
        }

    # ---- search -----------------------------------------------------------------

    def _search_focused(self) -> bool:
        return self.app.focused is self.query_one("#trajectory-search", Input)

    def action_open_search(self, animate: bool = True) -> None:
        search = self.query_one("#trajectory-search", TrajectorySearchInput)
        self.state.search_open = True
        search.value = self.state.query
        search.reveal(animate=animate)
        self.screen.set_focus(search, scroll_visible=False)

    def _close_search(self) -> None:
        self.state.search_open = False
        self.query_one("#trajectory-search", TrajectorySearchInput).conceal()
        self.focus_region(FocusRegion.TIMELINE)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "trajectory-search":
            return
        self.state.query = event.value.encode("utf-8")[:MAX_QUERY_BYTES].decode(
            "utf-8", errors="ignore"
        )
        if not self._search_refresh_pending:
            self._search_refresh_pending = self.call_after_refresh(self._refresh_search)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "trajectory-search":
            return
        event.stop()
        self._close_search()
        if self.state.selected_id not in self.projection.matched_ids:
            self.action_match(1)

    def _refresh_search(self) -> None:
        self._search_refresh_pending = False
        self.state.begin_search(self.state.query if self.controller is not None else "")
        self._refresh()
        if self._search_worker is not None and not self._search_worker.is_finished:
            self._search_worker.cancel()
        if self.controller is not None and self.state.query.strip():
            self._search_worker = self.run_worker(
                self._search_full_history(self.state.query),
                name="trajectory-search",
                group="trajectory-search",
                exclusive=True,
            )

    async def _search_full_history(self, query: str) -> None:
        await asyncio.sleep(TRAJECTORY_SEARCH_DEBOUNCE_SECONDS)
        if self.state.query == query and self.controller is not None:
            await self.controller.search_full_history(query, self.participant_id)

    # ---- widget messages ----------------------------------------------------------

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        panel = self.query_one("#trajectory-span-detail", SpanDetailPanel)
        if event.widget is panel or panel in event.widget.ancestors:
            self.state.focus_region = FocusRegion.DETAIL
        elif isinstance(event.widget, Timeline):
            self.state.focus_region = FocusRegion.TIMELINE
        self._update_footer()
        self._sync_focus_highlight()

    def on_descendant_blur(self, _event: events.DescendantBlur) -> None:
        self.call_after_refresh(self._sync_focus_highlight)

    def _sync_focus_highlight(self) -> None:
        """Tint the header above the timeline while the timeline holds focus."""
        if self.is_attached:
            timeline = self.query_one("#trajectory-timeline", Timeline)
            self.set_class(self.app.focused is timeline, "-timeline-focus")

    def on_timeline_span_clicked(self, message: TimelineSpanClicked) -> None:
        self._select(message.record_id)
        if message.open_details:
            self.focus_region(FocusRegion.DETAIL)

    def on_timeline_scrolled(self, message: TimelineScrolled) -> None:
        self.state.timeline_scroll = message.offset
        timeline = self.query_one("#trajectory-timeline", Timeline)
        if message.offset < timeline.tail_offset and self.state.follow_tail:
            self.state.pause_follow()
            self._update_footer()

    def on_span_detail_copy_requested(self, message: SpanDetailCopyRequested) -> None:
        self.run_worker(self._copy(message.text), name="trajectory-copy")

    def on_span_detail_participant_link_clicked(
        self, message: SpanDetailParticipantLinkClicked
    ) -> None:
        self._activate_participant_link(
            message.link, exact=message.exact, unresolved=message.unresolved
        )

    def _activate_participant_link(
        self, link: ParticipantLink, *, exact: bool, unresolved: bool
    ) -> None:
        if link.target_record_id is None and self._participant_link is not None:
            result = self._participant_link(link.participant_id)
            if inspect.isawaitable(result):
                self.run_worker(result, name="trajectory-participant-link")
            return
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
        self._refresh()
        if not self.select_and_reveal_record(record_id):
            self.notify("linked event could not be shown", severity="warning")


__all__ = [
    "ReturnToTree",
    "TrajectoryBackRequested",
    "TrajectoryCopyRequested",
    "TrajectoryParticipantSelected",
    "TrajectoryRetryRequested",
    "TrajectoryView",
]
