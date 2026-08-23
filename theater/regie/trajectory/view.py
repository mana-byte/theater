"""Standalone trajectory surface and contextual focus-region actions."""

from __future__ import annotations

import inspect
from bisect import bisect_left, bisect_right
from collections.abc import Awaitable, Callable

from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input
from textual.worker import Worker

from theater.regie.trajectory.constants import (
    INSPECTOR_RESIZE_STEP,
    MAX_QUERY_BYTES,
    SEARCH_HEIGHT,
    TIMELINE_HEIGHT,
    TRAJECTORY_HORIZONTAL_PADDING,
)
from theater.regie.trajectory.controller import TrajectoryController
from theater.regie.trajectory.enums import FilterDimension, FocusRegion, OrderMode
from theater.regie.trajectory.filter_panel import (
    FilterClearRequested,
    FilterPanel,
    FilterPanelClosed,
    FilterValueClicked,
)
from theater.regie.trajectory.inspector import (
    Inspector,
    InspectorMaximizeRequested,
    InspectorParticipantLinkClicked,
    InspectorResizeRequested,
    InspectorTabChanged,
)
from theater.regie.trajectory.ledger import (
    Ledger,
    LedgerGroupClicked,
    LedgerOlderClicked,
    LedgerRecordClicked,
    LedgerRecordHovered,
    LedgerRetryClicked,
)
from theater.regie.trajectory.ordering import TrajectoryOrdering, build_ordering
from theater.regie.trajectory.render import sanitize_text
from theater.regie.trajectory.search import FilterCounts, SearchCache, SearchResult, search_records
from theater.regie.trajectory.state import ParticipantTrajectoryState, TrajectoryStateStore
from theater.regie.trajectory.timeline import (
    Timeline,
    TimelineScrolled,
    TimelineSpanClicked,
    TimelineSpanHovered,
)
from theater.regie.trajectory.toolbar import ToolbarActionRequested, TrajectoryToolbar
from theater.trajectory import (
    TrajectoryGroup,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryPage,
    TrajectoryRecord,
    TrajectoryStatus,
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


class TrajectoryRetryRequested(Message):
    """A host without an injected controller can handle a retry request."""

    def __init__(self, participant_id: str) -> None:
        super().__init__()
        self.participant_id = participant_id


CopyRequest = Callable[[str], object | Awaitable[object]]
ParticipantLinkRequest = Callable[[str], object | Awaitable[object]]


class TrajectoryView(Vertical):
    """A fixed timeline, viewport ledger, filter chooser, and inspector drawer."""

    can_focus = True

    DEFAULT_CSS = f"""
    TrajectoryView {{
        width: 1fr;
        height: 1fr;
        min-width: 0;
        min-height: 0;
        layers: trajectory-base trajectory-overlay;
        background: $background;
        padding: 0 {TRAJECTORY_HORIZONTAL_PADDING};
    }}
    TrajectoryView > #trajectory-toolbar,
    TrajectoryView > #trajectory-timeline,
    TrajectoryView > #trajectory-search,
    TrajectoryView > #trajectory-ledger,
    TrajectoryView > #trajectory-inspector {{
        layer: trajectory-base;
    }}
    TrajectoryView > #trajectory-timeline {{
        width: 1fr;
        height: {TIMELINE_HEIGHT};
        min-height: {TIMELINE_HEIGHT};
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
    TrajectoryView > #trajectory-search {{
        width: 1fr;
        height: {SEARCH_HEIGHT};
        min-height: {SEARCH_HEIGHT};
        padding: 0 1;
        border: tall $panel;
        background: $panel;
    }}
    TrajectoryView > #trajectory-search:focus {{
        border: tall $accent;
        background: $accent 12%;
    }}
    TrajectoryView .-hidden {{
        display: none;
    }}
    TrajectoryView #trajectory-inspector.-closed {{
        display: none;
    }}
    TrajectoryView.-inspector-maximized > #trajectory-toolbar,
    TrajectoryView.-inspector-maximized > #trajectory-timeline,
    TrajectoryView.-inspector-maximized > #trajectory-filters,
    TrajectoryView.-inspector-maximized > #trajectory-search,
    TrajectoryView.-inspector-maximized > #trajectory-ledger {{
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
        self._search_result = SearchResult((), (), frozenset(), {}, FilterCounts())
        self._search_cache = SearchCache()
        self._search_key: tuple[object, ...] | None = None
        self._ordered_records: tuple[TrajectoryRecord, ...] = ()
        self._ordered_indices: dict[str, int] = {}
        self._visible_ids: tuple[str, ...] = ()
        self._visible_positions: tuple[int, ...] = ()
        self._visible_id_set: frozenset[str] = frozenset()
        self._visible_indices: dict[str, int] = {}
        self._search_refresh_pending = False
        self._load_worker: Worker[TrajectoryPage | None] | None = None

    def compose(self) -> ComposeResult:
        yield TrajectoryToolbar(id="trajectory-toolbar")
        yield Timeline(id="trajectory-timeline")
        yield FilterPanel(id="trajectory-filters", classes="-hidden")
        yield Input(
            placeholder="⌕ Search trajectory  /",
            id="trajectory-search",
        )
        yield Ledger(id="trajectory-ledger")
        yield Inspector(id="trajectory-inspector")

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
        self._refresh()
        if self.state.search_open:
            self._focus_search()
        elif self._focus_on_mount:
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

    def _make_search_key(self, records: tuple[TrajectoryRecord, ...]) -> tuple[object, ...]:
        record_key = tuple((record.record_id, record.revision) for record in records)
        groups = tuple(self._group_signature(group) for group in self.state.groups)
        return (
            record_key,
            self.state.query,
            frozenset(self.state.lane_filters),
            frozenset(self.state.kind_filters),
            frozenset(self.state.status_filters),
            frozenset(self.state.source_filters),
            groups,
            frozenset(self.state.collapsed_groups),
        )

    @staticmethod
    def _group_signature(group: TrajectoryGroup) -> tuple[object, ...]:
        return (
            group.group_id,
            group.label,
            group.record_ids,
            group.turn_id,
            group.step_id,
            tuple(TrajectoryView._group_signature(child) for child in group.children),
        )

    def _recompute_search(
        self, records: tuple[TrajectoryRecord, ...], ordering: TrajectoryOrdering
    ) -> None:
        self._ordered_records = records
        self._ordered_indices = {
            record.record_id: index for index, record in enumerate(self._ordered_records)
        }
        key = self._make_search_key(records)
        if key == self._search_key:
            return
        self._search_key = key
        self._search_result = search_records(
            records,
            query=self.state.query,
            lane_filters=self.state.lane_filters,
            kind_filters=self.state.kind_filters,
            status_filters=self.state.status_filters,
            source_filters=self.state.source_filters,
            groups=self.state.groups,
            collapsed_groups=self.state.collapsed_groups,
            cache=self._search_cache,
            ordering=ordering,
        )
        self._visible_ids = tuple(
            entry.record_id for entry in self._search_result.entries if entry.record_id is not None
        )
        self._visible_id_set = frozenset(self._visible_ids)
        self._visible_indices = {
            record_id: index for index, record_id in enumerate(self._visible_ids)
        }
        self._visible_positions = tuple(
            self._ordered_indices[record_id]
            for record_id in self._visible_ids
            if record_id in self._ordered_indices
        )

    @staticmethod
    def _set_class(widget: Widget, class_name: str, enabled: bool) -> bool:
        if widget.has_class(class_name) == enabled:
            return False
        widget.set_class(enabled, class_name)
        return True

    def _refresh(self, *, recompute: bool = True) -> None:
        if recompute or not self._ordered_records:
            ordering = build_ordering(tuple(self.state.records.values()), self.state.groups)
            records = ordering.records
            self._recompute_search(records, ordering)
        else:
            records = self._ordered_records
        if not self.is_mounted:
            return
        timeline = self.query_one("#trajectory-timeline", Timeline)
        ledger = self.query_one("#trajectory-ledger", Ledger)
        search = self.query_one("#trajectory-search", Input)
        if search.value != self.state.query and self.app.focused is not search:
            search.value = self.state.query
        timeline_offset: int | None = self.state.timeline_scroll
        if (
            not self.state.follow_tail
            and timeline.span_ids
            and timeline.horizontal_offset == timeline_offset
        ):
            timeline_offset = None
        timeline.update_records(
            records,
            matched_ids=self._search_result.matched_ids,
            hovered_id=self.state.hovered_id,
            selected_id=self.state.selected_id,
            duration_mode=self.state.order_mode is OrderMode.DURATION,
            scroll_offset=timeline_offset,
        )
        if self.state.follow_tail:
            timeline.scroll_to_tail()
        self.state.timeline_scroll = timeline.horizontal_offset
        ledger.update_rows(
            records,
            self._search_result,
            selected_id=self.state.selected_id,
            hovered_id=self.state.hovered_id,
            order_mode=self.state.order_mode,
            has_older=self.state.has_older,
            loading_older=self.state.loading_older,
            retry_message=self.state.retry_message if self.state.retry_kind else None,
        )
        self._sync_filter_panel(update_options=True)
        self._sync_inspector()
        if self.state.follow_tail:
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
        toolbar_message = sanitize_text(" · ".join(pieces[1:]))
        toolbar_message = toolbar_message.replace("\r", " ").replace("\n", " · ")
        toolbar = self.query_one("#trajectory-toolbar", TrajectoryToolbar)
        toolbar.update_state(
            status=status,
            message=toolbar_message,
            record_count=len(self.state.records),
            visible_count=len(self._search_result.records),
            active_filters=(
                len(self.state.lane_filters)
                + len(self.state.kind_filters)
                + len(self.state.status_filters)
                + len(self.state.source_filters)
            ),
            query=self.state.query,
            mode=self.state.order_mode,
            follow_tail=self.state.follow_tail,
            new_count=self.state.new_count,
        )

    @property
    def search_result(self) -> SearchResult:
        ordering = build_ordering(tuple(self.state.records.values()), self.state.groups)
        self._recompute_search(ordering.records, ordering)
        return self._search_result

    @property
    def active_region(self) -> FocusRegion:
        return self.state.focus_region

    def focus_region(self, region: FocusRegion) -> FocusRegion:
        self.state.focus_region = region
        if region is FocusRegion.INSPECTOR:
            self.state.inspector_open = True
        if self.is_mounted:
            if region is FocusRegion.INSPECTOR:
                self._sync_inspector()
            widget = {
                FocusRegion.TIMELINE: self.query_one("#trajectory-timeline", Timeline),
                FocusRegion.LEDGER: self.query_one("#trajectory-ledger", Ledger),
                FocusRegion.INSPECTOR: self.query_one("#trajectory-inspector", Inspector),
            }[region]
            widget.focus()
        return region

    def _sync_inspector(self) -> None:
        if not self.is_mounted:
            return
        inspector = self.query_one("#trajectory-inspector", Inspector)
        inspector.set_record(
            self.state.selected_record,
            tab=self.state.inspector_tab,
            render=self.state.inspector_open,
        )
        inspector.set_ratio(self.state.inspector_ratio)
        self._set_class(inspector, "-closed", not self.state.inspector_open)
        if inspector.maximized != self.state.inspector_maximized:
            inspector.toggle_maximize()
        self.set_class(self.state.inspector_maximized, "-inspector-maximized")

    def _sync_filter_panel(self, *, update_options: bool = False) -> None:
        if not self.is_mounted:
            return
        panel = self.query_one("#trajectory-filters", FilterPanel)
        if self.state.filters_open and update_options:
            panel.update_filters(
                self._search_result.counts,
                lanes=self.state.lane_filters,
                kinds=self.state.kind_filters,
                statuses=self.state.status_filters,
                sources=self.state.source_filters,
            )
        self._set_class(panel, "-open", self.state.filters_open)
        self._set_class(panel, "-hidden", not self.state.filters_open)

    def _sync_selection(self, *, scroll: bool = True) -> None:
        if not self.is_mounted:
            return
        record_id = self.state.selected_id
        self.query_one("#trajectory-timeline", Timeline).set_selected(record_id)
        self.query_one("#trajectory-ledger", Ledger).set_selected(record_id)
        self._sync_inspector()
        if scroll:
            self._scroll_to_record(record_id)
        self._update_status()

    def _scroll_to_record(self, record_id: str | None) -> None:
        if record_id is None or not self.is_mounted:
            return
        if record_id not in self._visible_id_set:
            return
        ledger = self.query_one("#trajectory-ledger", Ledger)
        ledger.scroll_to_record(record_id)
        timeline = self.query_one("#trajectory-timeline", Timeline)
        self.state.timeline_scroll = timeline.scroll_span_into_view(record_id)

    def _selected_visible_ids(self) -> tuple[str, ...]:
        return self._visible_ids

    def _pause_if_older(self, record_id: str | None) -> None:
        records = self._ordered_records
        if self.state.follow_tail and records and record_id != records[-1].record_id:
            self.state.pause_follow()

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
                self.controller.load_older(self.participant_id),
                name="trajectory-older",
                group="trajectory-older",
                exclusive=True,
            )
        if visible_ids:
            current = self._visible_indices.get(before) if before is not None else None
            if current is None:
                order_index = self._ordered_indices.get(before or "")
                if order_index is None:
                    target = 0 if delta > 0 else len(visible_ids) - 1
                elif delta > 0:
                    target = min(
                        len(visible_ids) - 1,
                        bisect_right(self._visible_positions, order_index),
                    )
                else:
                    target = max(0, bisect_left(self._visible_positions, order_index) - 1)
            else:
                target = max(0, min(len(visible_ids) - 1, current + delta))
            self.state.select(visible_ids[target])
            if delta < 0:
                self.state.pause_follow()
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
        self._pause_if_older(record_id)
        self.state.select(record_id)
        self._sync_selection()

    def action_timeline_next(self) -> None:
        self.focus_region(FocusRegion.TIMELINE)
        timeline = self.query_one("#trajectory-timeline", Timeline)
        record_id = timeline.move_span(1)
        self._pause_if_older(record_id)
        self.state.select(record_id)
        self._sync_selection()

    def action_collapse(self) -> None:
        record = self.state.selected_record
        if record is not None:
            path = self._search_result.path_for_record(record.record_id)
            if path:
                self.state.collapsed_groups.add(path[-1])
            self._refresh()

    def action_expand(self) -> None:
        record = self.state.selected_record
        if record is not None:
            path = self._search_result.path_for_record(record.record_id)
            if path:
                target = next(
                    (
                        group_id
                        for group_id in reversed(path)
                        if group_id in self.state.collapsed_groups
                    ),
                    path[-1],
                )
                self.state.collapsed_groups.discard(target)
            self._refresh()

    def action_toggle_mode(self) -> None:
        self.state.order_mode = (
            OrderMode.DURATION if self.state.order_mode is OrderMode.ORDER else OrderMode.ORDER
        )
        self._refresh(recompute=False)

    def action_open_search(self) -> None:
        self.state.search_open = True
        self.state.filters_open = False
        if self.is_mounted:
            search = self.query_one("#trajectory-search", Input)
            search.value = self.state.query
        self._sync_filter_panel()
        self._update_status()
        if self.is_mounted:
            self._focus_search()

    def _focus_search(self) -> None:
        if self.state.search_open and self.is_mounted:
            self.app.set_focus(self.query_one("#trajectory-search", Input), scroll_visible=False)

    def _close_search(self) -> None:
        self.state.search_open = False
        self.focus_region(self.state.focus_region)

    def action_toggle_filters(self) -> None:
        self.state.filters_open = not self.state.filters_open
        if self.state.filters_open:
            self.state.search_open = False
        self._sync_filter_panel(update_options=True)
        self._update_status()
        if self.state.filters_open and self.is_mounted:
            self.query_one("#trajectory-filters", FilterPanel).focus_options()

    def action_reset(self) -> None:
        self.state.reset_ui()
        if self.is_mounted:
            search = self.query_one("#trajectory-search", Input)
            search.value = ""
        self._refresh()
        self.focus_region(FocusRegion.LEDGER)
        if self.controller is not None:
            self.run_worker(
                self.controller.resume_follow(self.participant_id),
                name="trajectory-follow",
            )

    def action_oldest(self) -> None:
        self.state.pause_follow()
        visible = self._selected_visible_ids()
        self.state.select(visible[0] if visible else None)
        self.state.timeline_scroll = 0
        self._sync_selection()

    def action_tail(self) -> None:
        self.state.resume_follow()
        self._sync_selection()
        if self.controller is not None:
            self.run_worker(
                self.controller.resume_follow(self.participant_id), name="trajectory-follow"
            )

    def action_open_inspector(self) -> None:
        if self.state.selected_record is not None:
            self.state.inspector_open = True
            self.focus_region(FocusRegion.INSPECTOR)

    def action_cycle_region(self, delta: int = 1) -> None:
        regions = tuple(FocusRegion)
        index = regions.index(self.state.focus_region)
        self.focus_region(regions[(index + delta) % len(regions)])

    def action_resize_inspector(self, delta: float) -> None:
        self.state.set_ratio(self.state.inspector_ratio + delta)
        if self.is_mounted:
            self.query_one("#trajectory-inspector", Inspector).set_ratio(self.state.inspector_ratio)

    def action_toggle_inspector_maximize(self) -> None:
        if not self.state.inspector_open:
            return
        self.state.inspector_maximized = not self.state.inspector_maximized
        self._sync_inspector()

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
        text = self.query_one("#trajectory-inspector", Inspector).copy_text
        self.run_worker(self._copy(text), name="trajectory-copy")

    def action_retry(self) -> None:
        self.run_worker(self._retry(), name="trajectory-retry")

    def action_return_to_tree(self) -> None:
        self.post_message(ReturnToTree())

    def _handle_contextual_horizontal(self, delta: int) -> None:
        if self.state.focus_region is FocusRegion.TIMELINE:
            if delta < 0:
                self.action_timeline_previous()
            else:
                self.action_timeline_next()
        elif self.state.focus_region is FocusRegion.INSPECTOR:
            inspector = self.query_one("#trajectory-inspector", Inspector)
            self.state.inspector_tab = inspector.move_tab(delta)
        elif delta < 0:
            self.action_collapse()
        else:
            self.action_expand()

    def _search_owns_focus(self) -> bool:
        return self.is_mounted and self.app.focused is self.query_one("#trajectory-search", Input)

    def _filters_own_focus(self) -> bool:
        return (
            self.is_mounted and self.query_one("#trajectory-filters", FilterPanel).has_focus_within
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
            "shift+g": self.action_tail,
            "/": self.action_open_search,
            "slash": self.action_open_search,
            "f": self.action_toggle_filters,
            "d": self.action_toggle_mode,
            "m": self.action_toggle_inspector_maximize,
            "r": self.action_reset,
            "R": self.action_retry,
            "shift+r": self.action_retry,
            "y": self.action_copy,
            "enter": self.action_open_inspector,
            "tab": lambda: self.action_cycle_region(1),
            "shift+tab": lambda: self.action_cycle_region(-1),
            "escape": self.action_return_to_tree,
            "ctrl+left": lambda: self.action_resize_inspector(-INSPECTOR_RESIZE_STEP),
            "ctrl+right": lambda: self.action_resize_inspector(INSPECTOR_RESIZE_STEP),
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
            self.state.search_open = False

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "trajectory-search":
            return
        event.stop()
        self._close_search()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        widget = event.widget
        search = self.query_one("#trajectory-search", Input)
        if widget is search:
            self.state.search_open = True
            return
        regions = (
            (FocusRegion.TIMELINE, self.query_one("#trajectory-timeline", Timeline)),
            (FocusRegion.LEDGER, self.query_one("#trajectory-ledger", Ledger)),
            (FocusRegion.INSPECTOR, self.query_one("#trajectory-inspector", Inspector)),
        )
        ancestors = set(widget.ancestors)
        for region, container in regions:
            if widget is container or container in ancestors:
                self.state.focus_region = region
                break

    def on_timeline_span_hovered(self, message: TimelineSpanHovered) -> None:
        self.state.hovered_id = message.record_id
        if self.is_mounted:
            self.query_one("#trajectory-timeline", Timeline).set_hovered(message.record_id)
            self.query_one("#trajectory-ledger", Ledger).set_hovered(message.record_id)

    def on_timeline_span_clicked(self, message: TimelineSpanClicked) -> None:
        self._pause_if_older(message.record_id)
        self.state.select(message.record_id)
        self.state.focus_region = FocusRegion.TIMELINE
        self._sync_selection()

    def on_timeline_scrolled(self, message: TimelineScrolled) -> None:
        self.state.timeline_scroll = message.offset
        timeline = self.query_one("#trajectory-timeline", Timeline)
        if message.offset < timeline.tail_offset:
            self.state.pause_follow()
            self._update_status()

    def on_ledger_record_hovered(self, message: LedgerRecordHovered) -> None:
        self.state.hovered_id = message.record_id
        if self.is_mounted:
            self.query_one("#trajectory-timeline", Timeline).set_hovered(message.record_id)
            self.query_one("#trajectory-ledger", Ledger).set_hovered(message.record_id)

    def on_ledger_record_clicked(self, message: LedgerRecordClicked) -> None:
        self._pause_if_older(message.record_id)
        self.state.select(message.record_id)
        self.state.inspector_open = True
        self.state.focus_region = FocusRegion.INSPECTOR
        self._sync_selection()

    def on_ledger_group_clicked(self, message: LedgerGroupClicked) -> None:
        if message.group_id in self.state.collapsed_groups:
            self.state.collapsed_groups.remove(message.group_id)
        else:
            self.state.collapsed_groups.add(message.group_id)
        self._refresh()

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

    def on_toolbar_action_requested(self, message: ToolbarActionRequested) -> None:
        actions = {
            "search": self.action_open_search,
            "filters": self.action_toggle_filters,
            "mode": self.action_toggle_mode,
            "follow": self.action_tail,
        }
        action = actions.get(message.action)
        if action is not None:
            action()

    def on_inspector_participant_link_clicked(
        self, message: InspectorParticipantLinkClicked
    ) -> None:
        if self._participant_link is not None:
            self.run_worker(
                self._call_participant_link(message.participant_id),
                name="trajectory-participant-link",
            )
        else:
            self.post_message(TrajectoryParticipantSelected(message.participant_id))

    def on_inspector_resize_requested(self, message: InspectorResizeRequested) -> None:
        self.action_resize_inspector(message.delta)

    def on_inspector_maximize_requested(self, _message: InspectorMaximizeRequested) -> None:
        self.action_toggle_inspector_maximize()

    def on_inspector_tab_changed(self, message: InspectorTabChanged) -> None:
        self.state.inspector_tab = message.tab


__all__ = [
    "ReturnToTree",
    "TrajectoryCopyRequested",
    "TrajectoryParticipantSelected",
    "TrajectoryRetryRequested",
    "TrajectoryView",
]
