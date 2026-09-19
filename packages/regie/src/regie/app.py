"""The independent, public-SDK Régie Textual application."""

from __future__ import annotations

import contextlib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import ClassVar

from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding, BindingType
from textual.command import CommandPalette
from textual.containers import Vertical
from textual.dom import DOMNode
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import RichLog

from regie.animations.routes import RouteAnimationController
from regie.bus import DiagnosticBusController
from regie.bus_view import format_bus_line
from regie.contracts import PresentationOperations, RegieSettings
from regie.controllers.actions import ActionRecord, ActionState, OperationController
from regie.controllers.navigation import NavigationState
from regie.controllers.polling import RefreshGate
from regie.controllers.staging import StageController, StageOutcome, StageResult
from regie.controllers.surface import SurfaceController, SurfaceMode
from regie.controllers.usage import ActivateOutcome, SyncOutcome, UsagePanelState
from regie.dashboard import WelcomeDashboard
from regie.palette import (
    ResumeSessionCommand,
    ResumeSessionCommands,
    SpawnChoice,
    SpawnCommand,
    SpawnHarnessCommands,
    ViewCommands,
    spawn_approval,
    spawn_choices,
)
from regie.presentation import stageability
from regie.render import bounded_text
from regie.render.routing import await_highlight_cells
from regie.resume import ResumeCandidate, discover_resume_sessions
from regie.state import StateController
from regie.trajectory import TrajectoryController, TrajectoryView
from regie.ui_constants import (
    REGIE_COST_WINDOW_ROLLING_LABELS,
    REGIE_MICROCENTS_PER_DOLLAR,
    REGIE_PALETTE_KEYS_COMMAND_TITLE,
    REGIE_TRACE_ANIM_INTERVAL,
    REGIE_USAGE_METRIC_DOWN,
    REGIE_USAGE_METRIC_LEFT,
    REGIE_USAGE_METRIC_RIGHT,
    REGIE_USAGE_METRIC_UP,
    REGIE_USAGE_POLL_INTERVAL_SECONDS,
)
from regie.usage import UsageController
from regie.widgets import (
    ParticipantTree,
    PriceFooter,
    StatsFooter,
    StatusLine,
    TreeStack,
    UsageBreakdownPanel,
    UsageMetricTile,
    UsagePeriodBar,
)
from regie.widgets.prompts import (
    ControlPromptScreen,
    ResumePromptScreen,
    ResumeRequest,
    SettingsPromptScreen,
    SpawnDirectoryScreen,
)
from theater.frontend import (
    FrontendClient,
    FrontendClientError,
    FrontendResponseError,
    FrontendTransportError,
    StateProjection,
    StateSynchronizationError,
)
from theater.frontend.dto.catalogs import HarnessCatalogEntry


class RegieApp(App[None]):
    """A user-operable presentation client with no daemon or bridge startup path."""

    CSS = """
    Screen { layout: horizontal; }
    #sidebar { width: 52; min-width: 40; }
    #tree-stack { height: 1fr; layers: base overlay; }
    #participant-tree { height: 1fr; padding: 0; layer: base; }
    #bus { height: 12; padding: 0 1; }
    #right-surface { width: 1fr; min-width: 0; }
    #right-surface.-pane-staged { display: none; }
    #catalog-dashboard { height: 1fr; padding: 1 2; }
    #trajectory-view { height: 1fr; }
    #state-status { height: 1; padding: 0 1; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "cursor_down", "down", show=False),
        Binding("down", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("up", "cursor_up", "up", show=False),
        Binding("h", "cursor_left_or_trajectory", "trajectory", show=False),
        Binding("left", "cursor_left", "left", show=False),
        Binding("l", "cursor_right_or_focus", "focus", show=False),
        Binding("right", "cursor_right", "right", show=False),
        Binding("enter", "stage", "stage"),
        Binding("escape", "return_to_tree", "return", show=False),
        Binding("H,shift+h", "trajectory_previous", "older trajectory", show=False),
        Binding("L,shift+l", "trajectory_next", "newer trajectory", show=False),
        Binding("/", "trajectory_search", "search trajectory", show=False),
        Binding("s", "send", "send", show=False),
        Binding("a", "steer_session", "steer", show=False),
        Binding("i", "interrupt_session", "interrupt", show=False),
        Binding("f", "queue_followup", "followup", show=False),
        Binding("g", "update_session_settings", "settings", show=False),
        Binding("o", "spawn", "spawn"),
        Binding("r", "resume_sessions", "resume", show=False),
        Binding("v", "toggle_bus", "bus", show=False),
        Binding("x", "kill", "terminate", show=False),
        Binding("ctrl+p", "command_palette", "palette", show=False),
        Binding("q", "quit", "quit"),
    ]

    COMMANDS = App.COMMANDS | {SpawnCommand, ViewCommands, ResumeSessionCommand}

    title = "theater régie"

    def __init__(
        self,
        *,
        client: FrontendClient,
        settings: RegieSettings,
        presentation: PresentationOperations,
    ) -> None:
        super().__init__()
        self.client = client
        self.settings = settings
        self.presentation = presentation
        self._state = StateController(client)
        self._actions = OperationController(client)
        self._staging = StageController(settings, presentation)
        self._bus = DiagnosticBusController(client, batch=settings.bus_batch)
        self._animation_bus = DiagnosticBusController(client, batch=settings.bus_batch)
        self._animation_primed = False
        self._animation = RouteAnimationController()
        self._animation_timer: Timer | None = None
        self._trajectory = TrajectoryController(client, page_size=settings.trajectory_page_size)
        self._usage = UsageController(client)
        self._usage_panel = UsagePanelState()
        self._detailed_usage_loading = False
        self._navigation = NavigationState()
        self._surface = SurfaceController()
        self._sync_gate = RefreshGate()
        self._harnesses: tuple[HarnessCatalogEntry, ...] = ()
        self._resume_candidates: dict[str, ResumeCandidate] = {}
        self._bus_visible = settings.bus_visible
        self._closed = False

    @property
    def projection(self) -> StateProjection | None:
        return self._state.projection

    @property
    def actions(self) -> OperationController:
        return self._actions

    @property
    def selected_participant_id(self) -> str | None:
        return self._navigation.selected_id

    @property
    def bus_visible(self) -> bool:
        return self._bus_visible

    def compose(self) -> ComposeResult:
        with Vertical(id="sidebar"):
            with TreeStack(id="tree-stack"):
                yield ParticipantTree(
                    id="participant-tree",
                    startup_reveal=self.settings.startup_reveal,
                )
                yield UsageBreakdownPanel(id="usage-breakdown")
            yield UsagePeriodBar(id="usage-period")
            yield StatsFooter(id="stats-footer")
            yield PriceFooter(id="price-footer")
            bus = RichLog(id="bus", max_lines=200, wrap=False)
            bus.can_focus = False
            yield bus
        with Vertical(id="right-surface"):
            yield WelcomeDashboard(
                sentences=self.settings.dashboard_sentences,
                sentence_hold_seconds=self.settings.dashboard_sentence_hold_seconds,
                sentence_char_interval=self.settings.dashboard_sentence_char_interval,
                tip_hold_seconds=self.settings.dashboard_tip_hold_seconds,
                tip_char_interval=self.settings.dashboard_tip_char_interval,
                id="catalog-dashboard",
            )
            yield TrajectoryView(id="trajectory-view")
            yield StatusLine("Public API connected on demand", id="state-status")

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        """Replace Textual's redundant Keys entry with the dashboard hints."""
        for command in super().get_system_commands(screen):
            if command.title != REGIE_PALETTE_KEYS_COMMAND_TITLE:
                yield command

    async def on_mount(self) -> None:
        self.query_one("#sidebar").styles.width = self.settings.sidebar_width
        if self.settings.theme and self.settings.theme in self.available_themes:
            self.theme = self.settings.theme
        elif self.settings.theme:
            self.notify(f"unknown theme {self.settings.theme!r}", severity="warning")
        if self.settings.cost_window not in {"day", "week", "month", "year"}:
            self.notify("unknown cost window; using day", severity="warning")
        self._show_bus_visibility()
        self._sync_surface()
        await self._load_catalog()
        await self._refresh_usage()
        await self._initialize_projection()
        await self._refresh_animations()
        self.set_interval(self.settings.tree_interval, self._tick_synchronize)
        self.set_interval(self.settings.bus_interval, self._refresh_bus)
        self.set_interval(self.settings.bus_interval, self._refresh_animations)
        self.set_interval(REGIE_USAGE_POLL_INTERVAL_SECONDS, self._refresh_usage)
        self.set_interval(1.0, self._render_pending_actions)

    async def _load_catalog(self) -> bool:
        try:
            self._harnesses = (await self.client.catalogs.harnesses()).value.items
        except (FrontendClientError, FrontendResponseError, FrontendTransportError) as exc:
            self.notify(f"harness catalog unavailable: {exc}", severity="warning")
            return False
        self.query_one(WelcomeDashboard).show_catalog(self._harnesses)
        return True

    async def _initialize_projection(self) -> None:
        try:
            projection = await self._state.initialize()
        except (
            FrontendClientError,
            FrontendResponseError,
            FrontendTransportError,
            StateSynchronizationError,
        ) as exc:
            self._show_state_error(exc)
            return
        self._show_projection(projection)

    async def _refresh_usage(self) -> None:
        window = self.settings.cost_window
        if window not in {"day", "week", "month", "year"}:
            window = "day"
        try:
            usage = await self._usage.refresh(window=window)
        except (FrontendClientError, FrontendResponseError, FrontendTransportError) as exc:
            self._usage_panel.message = f"usage unavailable: {exc}"
            self._render_usage_breakdown()
            return
        summary = dict(usage.summary)
        windowed = summary.get("windowed")
        average = summary.get("average")
        totals = dict(windowed) if isinstance(windowed, dict) else dict(usage.totals)
        self.query_one("#stats-footer", StatsFooter).totals = totals
        self.query_one("#price-footer", PriceFooter).totals = totals
        if isinstance(average, dict):
            active_days = average.get("active_days", 0)
            try:
                days = float(active_days)
                daily_average = (
                    float(average.get("cost_microcents", 0)) / REGIE_MICROCENTS_PER_DOLLAR / days
                    if days > 0
                    else 0.0
                )
            except (TypeError, ValueError):
                daily_average = 0.0
            self.query_one("#price-footer", PriceFooter).daily_avg = daily_average
        self.query_one(
            "#usage-period", UsagePeriodBar
        ).period_label = REGIE_COST_WINDOW_ROLLING_LABELS[window]
        self._usage_panel.breakdown = dict(usage.by_harness)
        self._usage_panel.message = None
        self._render_usage_breakdown()

    def on_usage_metric_tile_hovered(self, message: UsageMetricTile.Hovered) -> None:
        self._usage_panel.pointer_metric = message.metric
        self._sync_usage_metric()

    def on_usage_metric_tile_clicked(self, message: UsageMetricTile.Clicked) -> None:
        self._usage_panel.pointer_metric = message.metric
        self._sync_usage_metric()
        self._toggle_usage_detailed()

    def on_usage_metric_tile_left(self, _message: UsageMetricTile.Left) -> None:
        self.call_after_refresh(self._hide_usage_breakdown_if_unhovered)

    def on_usage_breakdown_panel_left(self, _message: UsageBreakdownPanel.Left) -> None:
        self.call_after_refresh(self._hide_usage_breakdown_if_unhovered)

    def _render_usage_breakdown(self) -> None:
        metric = self._usage_panel.active_metric
        if metric is None or not self.is_running:
            return
        result = (
            self._usage_panel.detailed_breakdown
            if self._usage_panel.detailed
            else self._usage_panel.breakdown
        )
        message = (
            self._usage_panel.detailed_message
            if self._usage_panel.detailed
            else self._usage_panel.message
        )
        self.query_one("#usage-breakdown", UsageBreakdownPanel).render_state(
            metric,
            result=result,
            message=message,
            detailed=self._usage_panel.detailed,
        )

    def _activate_usage_metric(self, metric: str) -> None:
        outcome = self._usage_panel.activate(metric)
        if outcome is not ActivateOutcome.NO_CHANGE:
            for tile in self.query(UsageMetricTile):
                tile.set_class(tile.metric == metric, "-hot")
        panel = self.query_one("#usage-breakdown", UsageBreakdownPanel)
        panel.set_class(True, "-visible")
        if outcome is ActivateOutcome.FIRST_OPEN:
            self._constrain_usage_breakdown()
        self._render_usage_breakdown()

    def _sync_usage_metric(self) -> None:
        outcome = self._usage_panel.sync()
        if outcome is SyncOutcome.ACTIVATE:
            metric = self._usage_panel.resolve_metric()
            assert metric is not None
            self._activate_usage_metric(metric)
            return
        if outcome is SyncOutcome.CLOSE:
            for tile in self.query(UsageMetricTile):
                tile.set_class(False, "-hot")
            self._usage_panel.clear_active()
            self.query_one("#usage-breakdown", UsageBreakdownPanel).set_class(False, "-visible")

    def _toggle_usage_detailed(self) -> None:
        if self._usage_panel.active_metric is None:
            return
        self._usage_panel.detailed = not self._usage_panel.detailed
        if self._usage_panel.detailed and self._usage_panel.detailed_breakdown is None:
            self._usage_panel.detailed_message = "loading model details…"
            if not self._detailed_usage_loading:
                self._detailed_usage_loading = True
                self.run_worker(self._fetch_detailed_usage(), exclusive=False)
        self._render_usage_breakdown()

    async def _fetch_detailed_usage(self) -> None:
        try:
            result = await self._usage.detailed_breakdown()
        except (FrontendClientError, FrontendResponseError, FrontendTransportError, TypeError):
            self._usage_panel.detailed_breakdown = self._usage_panel.breakdown
            self._usage_panel.detailed_message = "model details unavailable"
        else:
            self._usage_panel.detailed_breakdown = result
            self._usage_panel.detailed_message = None
        finally:
            self._detailed_usage_loading = False
        self._render_usage_breakdown()

    def _constrain_usage_breakdown(self) -> None:
        panel = self.query_one("#usage-breakdown", UsageBreakdownPanel)
        panel.constrain_to_height(self.query_one("#tree-stack", TreeStack).size.height)

    def _hide_usage_breakdown_if_unhovered(self) -> None:
        node: DOMNode | None = self.mouse_over
        while node is not None:
            if isinstance(node, UsageMetricTile):
                self._usage_panel.pointer_metric = node.metric
                self._sync_usage_metric()
                return
            if isinstance(node, UsageBreakdownPanel):
                return
            node = node.parent
        self._usage_panel.pointer_metric = None
        self._sync_usage_metric()

    def _select_usage_metric(self, metric: str, *, origin: str | None = None) -> None:
        self._usage_panel.keyboard_metric = metric
        self._usage_panel.keyboard_origin = origin
        self.query_one(ParticipantTree).set_cursor_visible(False)
        self._sync_usage_metric()

    def _leave_usage_metrics(self) -> None:
        self._usage_panel.leave_keyboard()
        self.query_one(ParticipantTree).set_cursor_visible(True)
        self._sync_usage_metric()

    async def _tick_synchronize(self) -> None:
        await self._sync_gate.run(self._synchronize_projection)

    async def _synchronize_projection(self) -> None:
        previous = self._state.projection
        was_stale = previous is not None and previous.stale
        try:
            projection = await self._state.synchronize()
        except (
            FrontendClientError,
            FrontendResponseError,
            FrontendTransportError,
            StateSynchronizationError,
        ) as exc:
            self._show_state_error(exc)
            stale_projection = self._state.projection
            if stale_projection is not None:
                self._show_projection(stale_projection)
            return
        if was_stale and not projection.stale:
            await self._actions.refresh_pending()
        await self._refresh_catalog_if_dirty(projection)
        self._show_projection(projection)

    async def _refresh_catalog_if_dirty(self, projection: StateProjection) -> None:
        """Coalesce catalog invalidations into one bounded public refetch."""
        if not projection.catalog_dirty:
            return
        generation = projection.catalog_generation
        if await self._load_catalog():
            self._state.acknowledge_catalogs(generation)

    async def _refresh_bus(self) -> None:
        if not self._bus_visible:
            return
        try:
            rows = await self._bus.poll()
        except (FrontendClientError, FrontendResponseError, FrontendTransportError) as exc:
            self.notify(f"diagnostic bus unavailable: {exc}", severity="warning")
            return
        view = self.query_one("#bus", RichLog)
        for row in rows:
            variables = self.theme_variables if self.is_running else None
            view.write(format_bus_line(row, variables=variables))

    async def _refresh_animations(self) -> None:
        """Follow coordination events on a cursor independent of the bus panel."""
        try:
            rows = await self._animation_bus.poll()
        except (
            AttributeError,
            FrontendClientError,
            FrontendResponseError,
            FrontendTransportError,
        ):
            return
        if not self._animation_primed:
            self._animation_primed = True
            return
        if any(self._animation_needs_fresh_tree(row) for row in rows):
            await self._tick_synchronize()
        for row in rows:
            self._animate_bus_row(row)

    @staticmethod
    def _animation_needs_fresh_tree(row: object) -> bool:
        if not isinstance(row, dict):
            return False
        payload = row.get("payload")
        prompted_spawn = (
            row.get("kind") == "participant.created"
            and row.get("from_id")
            and isinstance(payload, Mapping)
            and payload.get("has_prompt") is True
        )
        return row.get("kind") == "job.await.start" or bool(prompted_spawn)

    def _animate_bus_row(self, row: object) -> None:
        if not isinstance(row, dict):
            return
        kind = row.get("kind")
        from_id = row.get("from_id") if isinstance(row.get("from_id"), str) else None
        to_id = row.get("to_id") if isinstance(row.get("to_id"), str) else None
        payload_value = row.get("payload")
        payload: Mapping[str, object] = payload_value if isinstance(payload_value, Mapping) else {}
        prompted_spawn = (
            kind == "participant.created" and from_id is not None and payload.get("has_prompt")
        )
        if kind in {"agent.send", "agent.steer", "agent.queue_followup"} or prompted_spawn:
            self.start_route_animation(from_id, to_id)
        elif kind == "job.await.start":
            self.start_await_animation(payload.get("token"), payload.get("handle"), from_id, to_id)
        elif kind == "job.await.end":
            self.stop_await_animation(payload.get("token"), payload.get("handle"), from_id, to_id)

    def start_route_animation(self, from_id: str | None, to_id: str | None) -> None:
        tree = self.query_one(ParticipantTree)
        if self._animation.start_route(tree.tree_lines, from_id, to_id).started:
            self._ensure_animation_timer()

    def start_await_animation(
        self,
        token: object,
        handle: object,
        from_id: str | None,
        to_id: str | None,
    ) -> None:
        tree = self.query_one(ParticipantTree)
        if self._animation.start_await(tree.tree_lines, token, handle, from_id, to_id).started:
            self._ensure_animation_timer()

    def stop_await_animation(
        self,
        token: object,
        handle: object,
        from_id: str | None,
        to_id: str | None,
    ) -> None:
        decision = self._animation.stop_await(token, handle, from_id, to_id)
        if decision.clear_overlays:
            self.query_one(ParticipantTree).set_overlays({})
        if decision.stop_timer:
            self._stop_animation_timer()

    def _ensure_animation_timer(self) -> None:
        if self._animation_timer is None:
            self._animation_timer = self.set_interval(
                REGIE_TRACE_ANIM_INTERVAL, self._tick_route_animations
            )

    def _tick_route_animations(self) -> None:
        tree = self.query_one(ParticipantTree)
        result = self._animation.tick(
            tree.tree_lines,
            tree.revision,
            await_highlight_cells,
        )
        tree.set_overlays(result.overlays)
        if result.stop_timer:
            self._stop_animation_timer()

    def _stop_animation_timer(self) -> None:
        if self._animation_timer is not None:
            self._animation_timer.stop()
            self._animation_timer = None

    def _show_projection(self, projection: StateProjection) -> None:
        stage_reasons = {
            participant.participant_id: eligibility.reason
            for participant in projection.participants.values()
            if not (
                eligibility := stageability(participant, projection.providers, self.presentation)
            ).allowed
            and eligibility.reason is not None
        }
        selected = self._navigation.reconcile(projection.participants)
        staged = self._staging.staged_target
        staged_id = self._participant_id_for_target(staged, projection)
        tree = self.query_one(ParticipantTree)
        selected = tree.show_projection(
            projection,
            participant_detail=self.settings.participant_detail,
            cwd_segments=self.settings.cwd_segments,
            stage_reasons=stage_reasons,
            selected_id=selected,
            staged_id=staged_id,
            trajectory_id=self._surface.trajectory_participant_id,
        )
        if selected is not None:
            self._navigation.select(selected)
        self.query_one("#state-status", StatusLine).update(
            "stale — reconnecting" if projection.stale else "live"
        )
        self._sync_surface()

    @staticmethod
    def _participant_id_for_target(
        target: object,
        projection: StateProjection,
    ) -> str | None:
        if target is None:
            return None
        for participant_id, participant in projection.participants.items():
            route = participant.terminal_route
            if route is None:
                continue
            identity = route.identity
            if (
                identity.provider_id == getattr(target, "provider_id", None)
                and identity.terminal_id == getattr(target, "terminal_id", None)
                and identity.terminal_incarnation == getattr(target, "terminal_incarnation", None)
            ):
                return participant_id
        return None

    def _show_state_error(self, exc: Exception) -> None:
        projection = self._state.projection
        state = "stale" if projection is not None else "unavailable"
        self.query_one("#state-status", StatusLine).update(f"{state}: {exc}")

    def _sync_surface(self) -> None:
        dashboard = self.query_one("#catalog-dashboard", WelcomeDashboard)
        trajectory = self.query_one("#trajectory-view", TrajectoryView)
        pane_staged = self._staging.staged_target is not None
        self.query_one("#right-surface", Vertical).set_class(pane_staged, "-pane-staged")
        dashboard.set_staged(pane_staged)
        dashboard.display = self._surface.mode is SurfaceMode.DASHBOARD
        trajectory.display = self._surface.mode is SurfaceMode.TRAJECTORY
        tree = self.query_one(ParticipantTree)
        projection = self._state.projection
        staged_id = (
            self._participant_id_for_target(self._staging.staged_target, projection)
            if projection is not None
            else None
        )
        tree.mark_surfaces(
            staged_id=staged_id,
            trajectory_id=self._surface.trajectory_participant_id,
        )

    def _show_bus_visibility(self) -> None:
        self.query_one("#bus", RichLog).display = self._bus_visible

    def capability_reason(self, participant_id: str, action: str) -> str | None:
        """Return a public capability refusal without inferring harness-specific policy."""
        projection = self._state.projection
        if projection is None:
            return "orchestration state has not loaded"
        if projection.stale:
            return "orchestration state is stale; wait for a fresh snapshot"
        participant = projection.participants.get(participant_id)
        if participant is None:
            return "participant is not in the active public projection"
        capability = participant.actions.get(action)
        if capability is None:
            return "action is not advertised by the public projection"
        if capability.supported and capability.route_available and capability.admissible:
            return None
        return capability.reason or capability.detail or "action is currently unavailable"

    def _control_refusal(self, participant_id: str, action: str) -> ActionRecord | None:
        reason = self.capability_reason(participant_id, action)
        return self._actions.refuse_locally(action, participant_id, reason) if reason else None

    def _selected_id(self) -> str | None:
        if self._usage_panel.in_footer:
            return None
        projection = self._state.projection
        selected = self._navigation.selected_id
        return selected if projection is not None and selected in projection.participants else None

    def select_participant(self, participant_id: str) -> None:
        """Select a stable participant ID from a pointer interaction."""
        projection = self._state.projection
        if projection is None or participant_id not in projection.participants:
            return
        if self._usage_panel.in_footer:
            self._leave_usage_metrics()
        self._navigation.select(participant_id)
        self.query_one(ParticipantTree).select(participant_id)

    def _move_selection(self, offset: int) -> None:
        if self._surface.mode is SurfaceMode.TRAJECTORY:
            self.query_one(TrajectoryView).move(offset)
            return
        selected = self.query_one(ParticipantTree).move(offset)
        if selected is not None:
            self._navigation.select(selected)

    def action_cursor_down(self) -> None:
        if self._surface.mode is SurfaceMode.TRAJECTORY:
            self._move_selection(1)
            return
        metric = self._usage_panel.keyboard_metric
        if metric is not None:
            target = REGIE_USAGE_METRIC_DOWN.get(metric)
            if target is not None:
                self._select_usage_metric(target, origin=metric)
            return
        tree = self.query_one(ParticipantTree)
        ids = tree.participant_ids
        if ids and tree.selected_id != ids[-1]:
            self._move_selection(1)
        else:
            self._select_usage_metric("input")

    def action_cursor_up(self) -> None:
        metric = self._usage_panel.keyboard_metric
        if metric is not None:
            if metric in REGIE_USAGE_METRIC_UP:
                target = self._usage_panel.keyboard_origin or REGIE_USAGE_METRIC_UP[metric]
                self._select_usage_metric(target)
            else:
                self._leave_usage_metrics()
            return
        self._move_selection(-1)

    def action_cursor_left(self) -> None:
        metric = self._usage_panel.keyboard_metric
        target = REGIE_USAGE_METRIC_LEFT.get(metric or "")
        if target is not None:
            self._select_usage_metric(target)

    def action_cursor_right(self) -> None:
        metric = self._usage_panel.keyboard_metric
        target = REGIE_USAGE_METRIC_RIGHT.get(metric or "")
        if target is not None:
            self._select_usage_metric(target)

    async def action_cursor_left_or_trajectory(self) -> None:
        if self._usage_panel.in_footer:
            self.action_cursor_left()
            return
        if self._surface.mode is SurfaceMode.TRAJECTORY:
            self.query_one(TrajectoryView).move(-1)
            return
        await self.action_toggle_trajectory()

    async def action_cursor_right_or_focus(self) -> None:
        if self._usage_panel.in_footer:
            self.action_cursor_right()
            return
        if self._surface.mode is SurfaceMode.TRAJECTORY:
            self.query_one(TrajectoryView).move(1)
            return
        await self.action_focus_stage()

    async def stage_participant(self, participant_id: str) -> StageResult | None:
        projection = self._state.projection
        if projection is None:
            return None
        if projection.stale:
            return StageResult(
                StageOutcome.UNAVAILABLE,
                None,
                "orchestration state is stale; wait for a fresh snapshot",
            )
        participant = projection.participants.get(participant_id)
        if participant is None:
            return None
        return await self._staging.stage(participant, projection.providers)

    async def action_stage(self) -> None:
        if self._usage_panel.in_footer:
            self._toggle_usage_detailed()
            return
        if self._surface.mode is SurfaceMode.TRAJECTORY:
            await self.action_return_to_tree()
        participant_id = self._selected_id()
        if participant_id is None:
            self.notify("nothing to stage", severity="warning")
            return
        self._show_stage_result(await self.stage_participant(participant_id))

    async def action_focus_stage(self) -> None:
        if not self._selection_is_staged():
            participant_id = self._selected_id()
            if participant_id is None:
                self.notify("nothing to stage", severity="warning")
                return
            self._show_stage_result(await self.stage_participant(participant_id))
            return
        result = await self._staging.focus()
        if result.outcome is StageOutcome.FOCUSED:
            self._set_status("staged terminal focused")
        else:
            self.notify(result.reason or "no terminal is staged", severity="warning")

    async def action_stage_and_focus_tmux(self) -> None:
        if not self._selection_is_staged():
            participant_id = self._selected_id()
            if participant_id is None:
                self.notify("nothing to stage", severity="warning")
                return
            result = await self.stage_participant(participant_id)
            self._show_stage_result(result)
            if result is None or result.outcome is not StageOutcome.STAGED:
                return
        await self.action_focus_stage()

    def _selection_is_staged(self) -> bool:
        projection = self._state.projection
        participant_id = self._selected_id()
        if projection is None or participant_id is None:
            return False
        participant = projection.participants.get(participant_id)
        if participant is None:
            return False
        target = stageability(participant, projection.providers, self.presentation).target
        return target is not None and target == self._staging.staged_target

    def _show_stage_result(self, result: StageResult | None) -> None:
        if result is None:
            return
        if result.outcome in {StageOutcome.STAGED, StageOutcome.UNSTAGED}:
            self._set_status(result.outcome.value)
        elif result.outcome is StageOutcome.UNSTAGEABLE:
            self.notify(result.reason or "terminal cannot be staged", severity="warning")
        elif result.outcome is not StageOutcome.FOCUSED:
            self.notify(result.reason or "stage unavailable", severity="warning")
        self._sync_surface()

    async def open_trajectory(self, participant_id: str) -> None:
        if self._staging.staged_target is not None:
            result = await self._staging.unstage()
            self._show_stage_result(result)
            if result.outcome is not StageOutcome.UNSTAGED:
                return
        try:
            state = await self._trajectory.open(participant_id)
        except (
            FrontendClientError,
            FrontendResponseError,
            FrontendTransportError,
            ValueError,
        ) as exc:
            self.notify(f"trajectory unavailable: {exc}", severity="warning")
            return
        self._surface.show_trajectory(participant_id)
        self.query_one(TrajectoryView).show_state(state)
        self._sync_surface()

    async def action_toggle_trajectory(self) -> None:
        participant_id = self._selected_id()
        if participant_id is None:
            self.notify("nothing to inspect", severity="warning")
            return
        if (
            self._surface.mode is SurfaceMode.TRAJECTORY
            and self._surface.trajectory_participant_id == participant_id
        ):
            await self.action_return_to_tree()
            return
        await self.open_trajectory(participant_id)

    async def action_stage_and_focus_trajectory(self) -> None:
        await self.action_toggle_trajectory()
        if self._surface.mode is SurfaceMode.TRAJECTORY:
            self.query_one(TrajectoryView).focus()

    async def action_trajectory_previous(self) -> None:
        if self._surface.mode is not SurfaceMode.TRAJECTORY:
            return
        try:
            state = await self._trajectory.load_older()
        except (
            FrontendClientError,
            FrontendResponseError,
            FrontendTransportError,
            ValueError,
        ) as exc:
            self.notify(f"older trajectory page unavailable: {exc}", severity="warning")
            return
        if state is not None:
            self.query_one(TrajectoryView).show_state(state)

    async def action_trajectory_next(self) -> None:
        if self._surface.mode is not SurfaceMode.TRAJECTORY:
            return
        try:
            await self._trajectory.follow_once()
        except (
            FrontendClientError,
            FrontendResponseError,
            FrontendTransportError,
            ValueError,
        ) as exc:
            self.notify(f"trajectory update unavailable: {exc}", severity="warning")
            return
        if self._trajectory.state is not None:
            self.query_one(TrajectoryView).show_state(self._trajectory.state)

    def action_trajectory_search(self) -> None:
        if self._surface.mode is not SurfaceMode.TRAJECTORY:
            return

        def receive(query: str | None) -> None:
            if query is not None:
                self.run_worker(self._search_trajectory(query), exclusive=False)

        self.push_screen(ControlPromptScreen("Search trajectory", "text to find"), receive)

    async def _search_trajectory(self, query: str) -> None:
        try:
            matches = await self._trajectory.search(query)
        except (
            FrontendClientError,
            FrontendResponseError,
            FrontendTransportError,
            ValueError,
        ) as exc:
            self.notify(f"trajectory search unavailable: {exc}", severity="warning")
            return
        self.query_one(TrajectoryView).search(query)
        self._set_status(f"trajectory search: {len(matches)} public matches")

    async def action_return_to_tree(self) -> None:
        try:
            await self._trajectory.close()
        except (FrontendClientError, FrontendResponseError, FrontendTransportError) as exc:
            self.notify(f"trajectory close unavailable: {exc}", severity="warning")
        self._surface.show_dashboard()
        self._sync_surface()

    def action_toggle_bus(self) -> None:
        self._bus_visible = not self._bus_visible
        self._show_bus_visibility()

    def action_resume_palette(self) -> None:
        self.push_screen(
            CommandPalette(
                providers=[ResumeSessionCommands],
                placeholder="Search for dead sessions…",
            )
        )

    def _spawn_choices(self) -> tuple[SpawnChoice, ...]:
        return spawn_choices(self._harnesses)

    def action_spawn(self) -> None:
        self.push_screen(
            CommandPalette(
                providers=[SpawnHarnessCommands],
                placeholder="Spawn a fresh session…",
            )
        )

    def spawn_harness(self, harness: str) -> None:
        """Start the bare, unparented session selected in the spawn palette."""
        approval = self._spawn_approval(harness)
        if approval is not None:
            self._start_action(self.submit_spawn(harness, "", approval, cwd=str(Path.cwd())))

    def spawn_harness_in_directory(self, harness: str) -> None:
        """Open a completing directory prompt for an otherwise bare spawn."""
        approval = self._spawn_approval(harness)
        if approval is None:
            return

        def receive(cwd: str | None) -> None:
            if cwd is not None:
                self._start_action(self.submit_spawn(harness, "", approval, cwd=cwd))

        self.push_screen(SpawnDirectoryScreen(harness, base_dir=Path.cwd()), receive)

    def _spawn_approval(self, harness: str) -> str | None:
        choice = next(
            (item for item in self._spawn_choices() if item.harness == harness),
            None,
        )
        if choice is None:
            self.notify("harness is not in the public catalog", severity="warning")
            return None
        if not choice.enabled:
            self.notify(choice.reason or "harness launch is unavailable", severity="warning")
            return None
        approval = spawn_approval(choice)
        if approval is None:
            detail = (
                "approval policies are absent from the public catalog"
                if choice.approvals is None
                else f"no safe automatic choice among {', '.join(choice.approvals) or 'none'}"
            )
            self.notify(f"cannot spawn {harness}: {detail}", severity="warning")
        return approval

    async def action_resume_sessions(self) -> None:
        """List a bounded public dead-session page before any resume mutation is offered."""
        try:
            discovery = await discover_resume_sessions(self.client)
        except (FrontendClientError, FrontendResponseError, FrontendTransportError) as exc:
            self.notify(f"resume sessions unavailable: {exc}", severity="warning")
            return
        self._resume_candidates = {
            candidate.participant_id: candidate for candidate in discovery.candidates
        }
        self.push_screen(
            ResumePromptScreen(
                discovery.candidates,
                more_available=discovery.more_available,
            ),
            self._submit_resume_request,
        )

    def open_resume_candidate(self, candidate: ResumeCandidate) -> None:
        if not candidate.available:
            self.notify(candidate.reason or "session cannot be resumed", severity="warning")
            return
        self._resume_candidates = {candidate.participant_id: candidate}
        self.push_screen(
            ResumePromptScreen(
                (candidate,),
                more_available=False,
                participant_id=candidate.participant_id,
            ),
            self._submit_resume_request,
        )

    def resume_dead_session(self, candidate: ResumeCandidate) -> None:
        """Resume the trusted session selected in the RC9-style palette."""
        if not candidate.available:
            self.notify(candidate.reason or "session cannot be resumed", severity="warning")
            return
        self._start_action(self.submit_resume(candidate, "", "manual"))

    def _submit_resume_request(self, request: ResumeRequest | None) -> None:
        if request is None:
            return
        candidate = self._resume_candidates.get(request.participant_id)
        if candidate is None:
            self.notify("session is not in the bounded public resume list", severity="warning")
            return
        if not candidate.available:
            self.notify(candidate.reason or "session cannot be resumed", severity="warning")
            return
        if request.approval not in {"manual", "edits", "yolo"}:
            self.notify("approval must be manual, edits, or yolo", severity="warning")
            return
        self._start_action(self.submit_resume(candidate, request.prompt, request.approval))

    def action_send(self) -> None:
        self._prompt_control("Send prompt", "message to deliver", self.submit_send)

    def action_steer_session(self) -> None:
        self._prompt_control(
            "Steer current work",
            "message to amend the current work",
            self.submit_steer,
        )

    def action_queue_followup(self) -> None:
        self._prompt_control("Queue followup", "message to deliver when idle", self.submit_followup)

    def _prompt_control(self, title: str, placeholder: str, submit: object) -> None:
        participant_id = self._selected_id()
        if participant_id is None:
            self.notify("no participant selected", severity="warning")
            return

        def receive(prompt: str | None) -> None:
            if prompt is None:
                return
            assert callable(submit)
            self._start_action(submit(participant_id, prompt))

        self.push_screen(ControlPromptScreen(title, placeholder), receive)

    def action_interrupt_session(self) -> None:
        participant_id = self._selected_id()
        if participant_id is None:
            self.notify("no participant selected", severity="warning")
            return
        self._start_action(self.submit_interrupt(participant_id))

    def action_update_session_settings(self) -> None:
        participant_id = self._selected_id()
        if participant_id is None:
            self.notify("no participant selected", severity="warning")
            return

        def receive(values: tuple[str, str] | None) -> None:
            if values is None or not any(values):
                return
            model, reasoning_effort = values
            self._start_action(
                self.submit_settings(
                    participant_id,
                    model=model or None,
                    reasoning_effort=reasoning_effort or None,
                )
            )

        self.push_screen(SettingsPromptScreen(), receive)

    def action_session_controls(self) -> None:
        participant_id = self._selected_id()
        if participant_id is None:
            self.notify("no participant selected", severity="warning")
            return
        self.run_worker(self._show_controls(participant_id), exclusive=False)

    async def _show_controls(self, participant_id: str) -> None:
        try:
            controls = (await self.client.controls.get(participant_id)).value
        except (FrontendClientError, FrontendResponseError, FrontendTransportError) as exc:
            self.notify(f"controls unavailable: {exc}", severity="warning")
            return
        self._set_status(f"controls: {controls}")

    def action_kill(self) -> None:
        participant_id = self._selected_id()
        if participant_id is None:
            self.notify("nothing to terminate", severity="warning")
            return
        self._start_action(self.submit_termination(participant_id))

    def action_terminate(self) -> None:
        self.action_kill()

    def _start_action(self, awaitable: object) -> None:
        assert hasattr(awaitable, "__await__")
        self.run_worker(self._observe_action(awaitable), exclusive=False)

    async def _observe_action(self, awaitable: object) -> None:
        assert hasattr(awaitable, "__await__")
        record = await awaitable
        assert isinstance(record, ActionRecord)
        self._show_action(record)

    def _render_pending_actions(self) -> None:
        records = self._actions.records
        if records:
            self._show_action(records[-1], announce=False)

    def _show_action(self, record: ActionRecord, *, announce: bool = True) -> None:
        detail = f": {record.detail}" if record.detail else ""
        self._set_status(f"{record.action} {record.state.value}{detail}")
        if announce and record.state is ActionState.REFUSED:
            self.notify(f"{record.action} refused{detail}", severity="warning")
        elif announce and record.state is ActionState.UNCERTAIN:
            self.notify(f"{record.action} outcome is uncertain{detail}", severity="warning")

    def _set_status(self, message: str) -> None:
        self.query_one("#state-status", StatusLine).update(bounded_text(message, limit=240))

    async def submit_send(self, participant_id: str, prompt: str) -> ActionRecord:
        if refusal := self._control_refusal(participant_id, "send"):
            return refusal
        return await self._actions.send(participant_id, prompt)

    async def submit_steer(self, participant_id: str, prompt: str) -> ActionRecord:
        if refusal := self._control_refusal(participant_id, "steer"):
            return refusal
        return await self._actions.steer(participant_id, prompt)

    async def submit_followup(self, participant_id: str, prompt: str) -> ActionRecord:
        if refusal := self._control_refusal(participant_id, "queue_followup"):
            return refusal
        return await self._actions.queue_followup(participant_id, prompt)

    async def submit_interrupt(self, participant_id: str) -> ActionRecord:
        if refusal := self._control_refusal(participant_id, "interrupt"):
            return refusal
        return await self._actions.interrupt(participant_id)

    async def submit_settings(
        self,
        participant_id: str,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> ActionRecord:
        if refusal := self._control_refusal(participant_id, "settings_update"):
            return refusal
        return await self._actions.update_settings(
            participant_id,
            model=model,
            reasoning_effort=reasoning_effort,
        )

    async def submit_termination(self, participant_id: str) -> ActionRecord:
        return await self._actions.terminate(participant_id)

    async def submit_spawn(
        self,
        harness: str,
        prompt: str,
        approval: str,
        *,
        cwd: str,
    ) -> ActionRecord:
        return await self._actions.spawn(harness, prompt, approval, cwd=cwd)

    async def submit_resume(
        self,
        candidate: ResumeCandidate,
        prompt: str,
        approval: str,
    ) -> ActionRecord:
        if not candidate.available or candidate.cwd is None or candidate.session_id is None:
            return self._actions.refuse_locally(
                "resume",
                candidate.participant_id,
                candidate.reason or "session cannot be resumed",
            )
        return await self._actions.resume(
            candidate.participant_id,
            harness=candidate.harness,
            cwd=candidate.cwd,
            session_id=candidate.session_id,
            approval=approval,
            prompt=prompt,
        )

    async def retry_action(self, action: str, target_id: str) -> ActionRecord | None:
        """Retry only an explicitly selected uncertain action with its retained key."""
        return await self._actions.retry(action, target_id)

    async def action_quit(self) -> None:
        """Restore local presentation before exit; this never terminates a terminal."""
        if not self._closed:
            self._closed = True
            with contextlib.suppress(Exception):
                await self._staging.close()
        self.exit()

    async def on_unmount(self) -> None:
        self._stop_animation_timer()
        await self._actions.close()
        with contextlib.suppress(Exception):
            await self._trajectory.close()
        if not self._closed:
            self._closed = True
            with contextlib.suppress(Exception):
                await self._staging.close()
        with contextlib.suppress(Exception):
            await self.client.close()


def catalog_names(harnesses: Iterable[HarnessCatalogEntry]) -> tuple[str, ...]:
    """Expose public catalog names for palette adapters without local discovery."""
    return tuple(entry.name for entry in harnesses if entry.launch_available)


__all__ = ["RegieApp", "catalog_names"]
