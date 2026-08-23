"""Standalone trajectory surface and contextual focus-region actions."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable

from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Input, Static
from textual.worker import Worker

from theater.regie.trajectory.controller import TrajectoryController
from theater.regie.trajectory.inspector import (
    Inspector,
    InspectorMessageRequested,
    InspectorParticipantLinkClicked,
)
from theater.regie.trajectory.ledger import (
    Ledger,
    LedgerGroupClicked,
    LedgerRecordClicked,
    LedgerRecordHovered,
)
from theater.regie.trajectory.models import (
    FocusRegion,
    ParticipantTrajectoryState,
    TrajectoryPage,
    TrajectoryStateStore,
)
from theater.regie.trajectory.render import sanitize_text
from theater.regie.trajectory.search import SearchResult, search_records
from theater.regie.trajectory.timeline import (
    Timeline,
    TimelineSpanClicked,
    TimelineSpanHovered,
    TimelineTooltipRequested,
)


class ReturnToTree(Message):
    """Esc asks the owning app to return focus to the participant tree."""


class TrajectoryCopyRequested(Message):
    """A bounded inspector tab is ready for the owning app's copy abstraction."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class TrajectoryParticipantSelected(Message):
    """A participant link asks the owning app to stage that tree leaf."""

    def __init__(self, participant_id: str) -> None:
        super().__init__()
        self.participant_id = participant_id


class TrajectoryMessageRequested(Message):
    """A participant link asks the owning app to offer a message action."""

    def __init__(self, participant_id: str, text: str) -> None:
        super().__init__()
        self.participant_id = participant_id
        self.text = text


CopyRequest = Callable[[str], object | Awaitable[object]]
ParticipantLinkRequest = Callable[[str], object]
MessageRequest = Callable[[str, str], object]


class TrajectoryView(Vertical):
    """A fixed timeline, virtualized ledger, and contextual inspector drawer."""

    can_focus = True

    DEFAULT_CSS = """
    TrajectoryView {
        width: 1fr;
        height: 1fr;
        min-width: 0;
        min-height: 0;
        background: $background;
    }
    TrajectoryView > #trajectory-status {
        width: 1fr;
        height: 2;
        min-height: 2;
        padding: 0 1;
    }
    TrajectoryView > #trajectory-ledger-scroll {
        width: 1fr;
        height: 1fr;
        min-height: 0;
        overflow-y: auto;
    }
    TrajectoryView > #trajectory-search {
        dock: top;
        width: 1fr;
        height: 3;
        margin: 0 1;
    }
    TrajectoryView .-hidden {
        display: none;
    }
    TrajectoryView #trajectory-inspector.-closed {
        display: none;
    }
    """

    def __init__(
        self,
        participant_id: str,
        *,
        controller: TrajectoryController | None = None,
        state_store: TrajectoryStateStore | None = None,
        copy_request: CopyRequest | None = None,
        participant_link: ParticipantLinkRequest | None = None,
        message_request: MessageRequest | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.participant_id = participant_id
        self.controller = controller
        self.state_store = state_store or (
            controller.state_store if controller else TrajectoryStateStore()
        )
        self.state = self.state_store.get(participant_id)
        self._copy_request = copy_request
        self._participant_link = participant_link
        self._message_request = message_request
        self._unsubscribe: Callable[[], None] | None = None
        from theater.regie.trajectory.search import FilterCounts

        self._search_result = SearchResult((), (), frozenset(), {}, FilterCounts())
        self._tooltip_text = ""
        self._load_worker: Worker[TrajectoryPage | None] | None = None

    def compose(self) -> ComposeResult:
        yield Static("Loading trajectory…", markup=False, id="trajectory-status")
        yield Timeline(id="trajectory-timeline")
        with VerticalScroll(id="trajectory-ledger-scroll"):
            yield Ledger(id="trajectory-ledger")
        yield Input(placeholder="Search trajectory", id="trajectory-search", classes="-hidden")
        yield Inspector(id="trajectory-inspector")

    def on_mount(self) -> None:
        if self.controller is not None:
            self._unsubscribe = self.controller.subscribe(self._controller_state_changed)
            self._load_worker = self.run_worker(
                self.controller.open(self.participant_id),
                name=f"trajectory-open-{self.participant_id}",
                exclusive=True,
            )
        self._refresh()
        self.focus_region(self.state.focus_region)

    def on_unmount(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._load_worker is not None and not self._load_worker.is_finished:
            self._load_worker.cancel()

    def _controller_state_changed(self, state: ParticipantTrajectoryState) -> None:
        if state.participant_id != self.participant_id:
            return
        self.state = state
        if self.is_mounted:
            self._refresh()

    def _refresh(self) -> None:
        self._search_result = search_records(
            self.state.record_list,
            query=self.state.query,
            lane_filters=self.state.lane_filters,
            kind_filters=self.state.kind_filters,
            status_filters=self.state.status_filters,
            source_filters=self.state.source_filters,
            groups=self.state.groups,
            collapsed_groups=self.state.collapsed_groups,
        )
        if not self.is_mounted:
            return
        timeline = self.query_one("#trajectory-timeline", Timeline)
        ledger = self.query_one("#trajectory-ledger", Ledger)
        inspector = self.query_one("#trajectory-inspector", Inspector)
        timeline.update_records(
            self.state.record_list,
            matched_ids=self._search_result.matched_ids,
            hovered_id=self.state.hovered_id,
        )
        ledger.update_rows(
            self.state.record_list,
            self._search_result,
            selected_id=self.state.selected_id,
            hovered_id=self.state.hovered_id,
            order_mode=self.state.order_mode,
            retry_message=self.state.retry_message if self.state.retry_kind else None,
        )
        inspector.set_record(self.state.selected_record, tab=self.state.inspector_tab)
        inspector.set_ratio(self.state.inspector_ratio)
        inspector.set_class(not self.state.inspector_open, "-closed")
        if inspector.maximized != self.state.inspector_maximized:
            inspector.toggle_maximize()
        self._update_status()

    def _update_status(self) -> None:
        status = self.state.panel.status.value
        message = self.state.panel.message or self.state.stale_message
        pieces = [status]
        if message:
            pieces.append(message)
        if self.state.follow_tail:
            pieces.append("following")
        elif self.state.new_count:
            pieces.append(f"↓ {self.state.new_count} new events · G to follow")
        if self.state.query:
            pieces.append(f"search: {self.state.query}")
        if self.state.reload_required:
            pieces.append("loaded window bounded; reload marker")
        if self.state.truncated_by_bytes:
            pieces.append("page byte cap reached")
        if self.state.filters_open:
            pieces.append(self._filter_counts_text())
        if self._tooltip_text:
            pieces.append(self._tooltip_text)
        self.query_one("#trajectory-status", Static).update(
            sanitize_text(" · ".join(pieces)), layout=False
        )

    def _filter_counts_text(self) -> str:
        counts = self._search_result.counts
        return (
            f"lanes {sum(counts.lanes.values())} · kinds {sum(counts.kinds.values())} · "
            f"statuses {sum(counts.statuses.values())} · sources {sum(counts.sources.values())}"
        )

    @property
    def search_result(self) -> SearchResult:
        return self._search_result

    @property
    def active_region(self) -> FocusRegion:
        return self.state.focus_region

    def focus_region(self, region: FocusRegion) -> FocusRegion:
        self.state.focus_region = region
        if region is FocusRegion.INSPECTOR:
            self.state.inspector_open = True
        if self.is_mounted:
            widget = {
                FocusRegion.TIMELINE: self.query_one("#trajectory-timeline", Timeline),
                FocusRegion.LEDGER: self.query_one("#trajectory-ledger", Ledger),
                FocusRegion.INSPECTOR: self.query_one("#trajectory-inspector", Inspector),
            }[region]
            widget.focus()
            if region is FocusRegion.INSPECTOR:
                widget.remove_class("-closed")
        return region

    def _scroll_to_record(self, record_id: str | None) -> None:
        if record_id is None or not self.is_mounted:
            return
        if record_id not in self._search_result.record_ids:
            return
        for line, entry in enumerate(self._search_result.entries):
            if entry.record_id == record_id:
                self.query_one("#trajectory-ledger-scroll", VerticalScroll).scroll_to(
                    y=line,
                    animate=False,
                )
                return

    def _selected_visible_ids(self) -> tuple[str, ...]:
        return self._search_result.record_ids

    def _select(self, delta: int) -> None:
        before = self.state.selected_id
        visible_ids = self._selected_visible_ids()
        if (
            delta < 0
            and before
            and visible_ids
            and before == visible_ids[0]
            and self.state.has_older
            and self.controller is not None
            and not self.state.loading_older
        ):
            self.run_worker(
                self.controller.load_older(self.participant_id), name="trajectory-older"
            )
        self.state.move_selection(delta, self._selected_visible_ids())
        self._refresh()
        self._scroll_to_record(self.state.selected_id)

    def action_select_next(self) -> None:
        self._select(1)

    def action_select_previous(self) -> None:
        self._select(-1)

    def action_timeline_previous(self) -> None:
        self.focus_region(FocusRegion.TIMELINE)
        record_id = self.query_one("#trajectory-timeline", Timeline).move_span(-1)
        self.state.select(record_id)
        self._refresh()

    def action_timeline_next(self) -> None:
        self.focus_region(FocusRegion.TIMELINE)
        record_id = self.query_one("#trajectory-timeline", Timeline).move_span(1)
        self.state.select(record_id)
        self._refresh()

    def action_collapse(self) -> None:
        record = self.state.selected_record
        if record is not None:
            self.state.collapsed_groups.add(record.group_id)
            self._refresh()

    def action_expand(self) -> None:
        record = self.state.selected_record
        if record is not None:
            self.state.collapsed_groups.discard(record.group_id)
            self._refresh()

    def action_toggle_mode(self) -> None:
        from theater.regie.trajectory.models import OrderMode

        self.state.order_mode = (
            OrderMode.DURATION if self.state.order_mode == OrderMode.ORDER else OrderMode.ORDER
        )
        self._refresh()

    def action_open_search(self) -> None:
        self.state.search_open = True
        if self.is_mounted:
            search = self.query_one("#trajectory-search", Input)
            search.remove_class("-hidden")
            search.value = self.state.query
            search.focus()
        self._refresh()

    def action_toggle_filters(self) -> None:
        self.state.filters_open = not self.state.filters_open
        self._refresh()

    def action_reset(self) -> None:
        self.state.reset_ui()
        if self.is_mounted:
            search = self.query_one("#trajectory-search", Input)
            search.value = ""
            search.add_class("-hidden")
        self._refresh()

    def action_oldest(self) -> None:
        self.state.pause_follow()
        self.state.select(self._selected_visible_ids()[0] if self._selected_visible_ids() else None)
        self.state.timeline_scroll = 0
        self._refresh()
        self._scroll_to_record(self.state.selected_id)

    def action_tail(self) -> None:
        self.state.resume_follow()
        self._refresh()
        self._scroll_to_record(self.state.selected_id)
        if self.controller is not None:
            self.run_worker(
                self.controller.resume_follow(self.participant_id), name="trajectory-follow"
            )

    def action_open_inspector(self) -> None:
        if self.state.selected_record is not None:
            self.state.inspector_open = True
            self.focus_region(FocusRegion.INSPECTOR)
            self._refresh()

    def action_cycle_region(self, delta: int = 1) -> None:
        regions = tuple(FocusRegion)
        index = regions.index(self.state.focus_region)
        self.focus_region(regions[(index + delta) % len(regions)])
        self._refresh()

    def action_resize_inspector(self, delta: float) -> None:
        self.state.set_ratio(self.state.inspector_ratio + delta)
        self._refresh()

    def action_toggle_inspector_maximize(self) -> None:
        self.state.inspector_maximized = not self.state.inspector_maximized
        self._refresh()

    async def _load_older(self) -> None:
        if self.controller is not None:
            await self.controller.load_older(self.participant_id)

    async def _retry(self) -> None:
        if self.controller is not None:
            await self.controller.retry(self.participant_id)

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

    async def _call_message_request(self, participant_id: str, text: str) -> None:
        if self._message_request is not None:
            result = self._message_request(participant_id, text)
            if inspect.isawaitable(result):
                await result

    def action_copy(self) -> None:
        text = self.query_one("#trajectory-inspector", Inspector).copy_text
        self.run_worker(self._copy(text), name="trajectory-copy")

    def action_retry(self) -> None:
        self.run_worker(self._retry(), name="trajectory-retry")

    def action_return_to_tree(self) -> None:
        self.post_message(ReturnToTree())

    def _handle_contextual_horizontal(self, delta: int) -> None:
        if self.state.focus_region == FocusRegion.TIMELINE:
            if delta < 0:
                self.action_timeline_previous()
            else:
                self.action_timeline_next()
        elif self.state.focus_region == FocusRegion.INSPECTOR:
            inspector = self.query_one("#trajectory-inspector", Inspector)
            self.state.inspector_tab = inspector.move_tab(delta)
            self._refresh()
        elif delta < 0:
            self.action_collapse()
        else:
            self.action_expand()

    def on_key(self, event: events.Key) -> None:
        actions: dict[str, Callable[[], None]] = {
            "j": self.action_select_next,
            "down": self.action_select_next,
            "k": self.action_select_previous,
            "up": self.action_select_previous,
            "h": lambda: self._handle_contextual_horizontal(-1),
            "left": lambda: self._handle_contextual_horizontal(-1),
            "l": lambda: self._handle_contextual_horizontal(1),
            "right": lambda: self._handle_contextual_horizontal(1),
            "g": self.action_oldest,
            "G": self.action_tail,
            "/": self.action_open_search,
            "slash": self.action_open_search,
            "f": self.action_toggle_filters,
            "d": self.action_toggle_mode,
            "r": self.action_reset,
            "R": self.action_retry,
            "y": self.action_copy,
            "enter": self.action_open_inspector,
            "tab": lambda: self.action_cycle_region(1),
            "shift+tab": lambda: self.action_cycle_region(-1),
            "escape": self.action_return_to_tree,
        }
        action = actions.get(event.key)
        if action is None:
            return
        event.stop()
        action()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "trajectory-search":
            return
        self.state.query = event.value
        self._refresh()

    def on_timeline_span_hovered(self, message: TimelineSpanHovered) -> None:
        self.state.hovered_id = message.record_id
        self._refresh()

    def on_timeline_span_clicked(self, message: TimelineSpanClicked) -> None:
        self.state.select(message.record_id)
        self.state.focus_region = FocusRegion.TIMELINE
        self._refresh()
        self._scroll_to_record(message.record_id)

    def on_timeline_tooltip_requested(self, message: TimelineTooltipRequested) -> None:
        self._tooltip_text = message.text
        self._update_status()

    def on_ledger_record_hovered(self, message: LedgerRecordHovered) -> None:
        self.state.hovered_id = message.record_id
        self._refresh()

    def on_ledger_record_clicked(self, message: LedgerRecordClicked) -> None:
        self.state.select(message.record_id)
        self.state.inspector_open = True
        self.state.focus_region = FocusRegion.INSPECTOR
        self._refresh()

    def on_ledger_group_clicked(self, message: LedgerGroupClicked) -> None:
        if message.group_id in self.state.collapsed_groups:
            self.state.collapsed_groups.remove(message.group_id)
        else:
            self.state.collapsed_groups.add(message.group_id)
        self._refresh()

    def on_inspector_participant_link_clicked(
        self, message: InspectorParticipantLinkClicked
    ) -> None:
        self.run_worker(
            self._call_participant_link(message.participant_id),
            name="trajectory-participant-link",
        )
        self.post_message(TrajectoryParticipantSelected(message.participant_id))

    def on_inspector_message_requested(self, message: InspectorMessageRequested) -> None:
        self.run_worker(
            self._call_message_request(message.participant_id, message.text),
            name="trajectory-message-request",
        )
        self.post_message(TrajectoryMessageRequested(message.participant_id, message.text))


__all__ = [
    "ReturnToTree",
    "TrajectoryCopyRequested",
    "TrajectoryMessageRequested",
    "TrajectoryParticipantSelected",
    "TrajectoryView",
]
