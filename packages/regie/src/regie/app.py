"""The independent, public-SDK Régie Textual application."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable, Mapping
from functools import partial
from pathlib import Path
from time import monotonic
from typing import ClassVar

from rich.text import Text
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding, BindingType
from textual.command import CommandPalette
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.dom import DOMNode
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import RichLog

from regie.animations.routes import RouteAnimationController
from regie.bus import DiagnosticBusController
from regie.bus_view import format_bus_line
from regie.client_pool import FrontendClientPool
from regie.contracts import (
    LocalPresentationTarget,
    PresentationOperations,
    RegieSettings,
    UnmanagedPane,
)
from regie.controllers.actions import ActionRecord, OperationController
from regie.controllers.controls import describe_action, format_controls_report
from regie.controllers.navigation import NavigationState
from regie.controllers.polling import RefreshGate
from regie.controllers.staging import StageController, StageOutcome, StageResult
from regie.controllers.startup import start_reader
from regie.controllers.surface import SurfaceController, SurfaceMode
from regie.controllers.transcripts import (
    TranscriptBindingController,
    TranscriptBindState,
)
from regie.controllers.usage import ActivateOutcome, FetchAccept, SyncOutcome, UsagePanelState
from regie.dashboard import WelcomeDashboard
from regie.latency import action_phase, startup_milestone, startup_phase, startup_stage
from regie.observability import lag_monitor, log_exception
from regie.palette import (
    ResumeSessionCommand,
    ResumeSessionCommands,
    RetryActionCommand,
    SpawnChoice,
    SpawnCommand,
    SpawnHarnessCommands,
    TranscriptCandidateCommands,
    TranscriptRecoveryCommand,
    ViewCommands,
    spawn_approval,
    spawn_choices,
)
from regie.presentation import stageability
from regie.render.layout import Key
from regie.render.routing import await_highlight_cells
from regie.resume import ResumeCandidate, ResumeDiscovery, discover_resume_sessions
from regie.state import StateController
from regie.trajectory.adapter import TrajectoryFollowAdapter, TrajectoryQueryAdapter
from regie.trajectory.domain.location import TrajectoryLocationResolution
from regie.trajectory.rich import (
    ReturnToTree,
    TrajectoryBackRequested,
    TrajectoryController,
    TrajectoryCopyRequested,
    TrajectoryNavigationHistory,
    TrajectoryParticipantSelected,
    TrajectoryRetryRequested,
    TrajectoryStateStore,
    TrajectoryView,
)
from regie.trajectory.ui_constants import TOOLTIP_DELAY
from regie.ui_constants import (
    REGIE_CONTROLS_REPORT_TIMEOUT_SECONDS,
    REGIE_COST_WINDOW_LABELS,
    REGIE_MICROCENTS_PER_DOLLAR,
    REGIE_PALETTE_KEYS_COMMAND_TITLE,
    REGIE_RETURN_SIGNAL_TEXTUAL,
    REGIE_TRACE_ANIM_INTERVAL,
    REGIE_UNMANAGED_POLL_INTERVAL_SECONDS,
    REGIE_USAGE_AVERAGE_WINDOW_DAYS,
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
    TranscriptTransferScreen,
)
from theater.frontend import (
    FrontendClient,
    FrontendClientError,
    FrontendResponseError,
    FrontendTransportError,
    StateProjection,
    StateSynchronizationError,
    TranscriptCandidate,
    local_harness_catalog,
)
from theater.frontend.dto.catalogs import HarnessCatalogEntry

logger = logging.getLogger("regie")


class RegieApp(App[None]):
    """A user-operable presentation client with no daemon or bridge startup path."""

    TOOLTIP_DELAY = TOOLTIP_DELAY

    CSS = """
    Screen { layout: horizontal; }
    #sidebar { width: 52; min-width: 40; }
    #tree-stack { height: 1fr; layers: base overlay; }
    #participant-tree { height: 1fr; padding: 0; layer: base; }
    #bus { height: 18; padding: 1 2; scrollbar-size: 0 0; }
    #right-surface { width: 1fr; height: 1fr; min-width: 0; min-height: 0; }
    #right-surface.-pane-staged { display: none; }
    #catalog-dashboard { height: 1fr; }
    #trajectory-view { height: 1fr; }
    .log { background: $surface; }
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
        Binding(REGIE_RETURN_SIGNAL_TEXTUAL, "return_to_tree", show=False, priority=True),
        Binding("H,shift+h", "stage_and_focus_trajectory", "open trajectory", show=False),
        Binding("L,shift+l", "stage_and_focus_tmux", "open agent", show=False),
        Binding("s", "send", "send", show=False),
        Binding("a", "steer_session", "steer", show=False),
        Binding("i", "interrupt_session", "interrupt", show=False),
        Binding("f", "queue_followup", "followup", show=False),
        Binding("g", "update_session_settings", "settings", show=False),
        Binding("o", "spawn", "spawn"),
        Binding("r", "resume_sessions", "resume", show=False),
        Binding("v", "toggle_bus", "bus", show=False),
        Binding("x", "kill", "kill"),
        Binding("ctrl+p", "command_palette", "palette", show=False),
        Binding("q", "quit", "quit"),
    ]

    COMMANDS = App.COMMANDS | {
        ResumeSessionCommand,
        RetryActionCommand,
        SpawnCommand,
        TranscriptRecoveryCommand,
        ViewCommands,
    }

    title = "theater régie"

    def __init__(
        self,
        *,
        client: FrontendClient,
        settings: RegieSettings,
        presentation: PresentationOperations,
    ) -> None:
        self._startup_started_at = monotonic()
        self._initial_projection_pending = True
        super().__init__()
        self.client = client
        self._clients = FrontendClientPool(client)
        self.settings = settings
        self.presentation = presentation
        self._state = StateController(self._clients.state)
        self._actions = OperationController(
            self._clients.controls,
            client_factory=self._clients.action_client,
            on_change=self._action_changed,
        )
        self._staging = StageController(settings, presentation)
        self._bus = DiagnosticBusController(self._clients.bus, batch=settings.bus_batch)
        self._animation_bus = DiagnosticBusController(
            self._clients.animation_bus, batch=settings.bus_batch
        )
        self._animation_primed = False
        self._animation = RouteAnimationController()
        self._animation_timer: Timer | None = None
        self._trajectory_states = TrajectoryStateStore(page_size=settings.trajectory_page_size)
        self._trajectory = TrajectoryController(
            TrajectoryQueryAdapter(self._clients.trajectory_query),
            TrajectoryFollowAdapter(self._clients.trajectory_follow),
            state_store=self._trajectory_states,
        )
        self._trajectory_view_widget: TrajectoryView | None = None
        self._trajectory_navigation = TrajectoryNavigationHistory()
        self._usage = UsageController(self._clients.usage)
        self._usage_panel = UsagePanelState()
        self._transcript_bindings = TranscriptBindingController(
            self._clients.transcripts,
            client_factory=self._clients.transcript_client,
        )
        self._navigation = NavigationState()
        self._surface = SurfaceController()
        self._sync_gate = RefreshGate()
        # Every fixed-purpose SDK client still has one interactive lane.  Keep
        # repeated reads for that purpose ordered when a timer, palette, or
        # keybinding fires again before the preceding request has returned.
        self._catalog_lock = asyncio.Lock()
        self._controls_inspection_lock = asyncio.Lock()
        self._resume_discovery_lock = asyncio.Lock()
        self._transcript_candidates_lock = asyncio.Lock()
        self._harnesses: tuple[HarnessCatalogEntry, ...] = ()
        self._resume_candidates: dict[str, ResumeCandidate] = {}
        self._transcript_recovery_target: str | None = None
        self._unmanaged: tuple[UnmanagedPane, ...] | None = None
        self._unmanaged_polled_at: float | None = None
        self._bus_visible = settings.bus_visible
        self._last_state_error: tuple[str, str] | None = None
        self._last_action_signature: tuple[object, ...] | None = None
        self._action_signatures: dict[str, tuple[object, ...]] = {}
        self._reconciled_actions: set[str] = set()
        self._reconciling_actions: set[str] = set()
        self._lag_stopping = asyncio.Event()
        self._lag_task: asyncio.Task[None] | None = None
        self._startup_task: asyncio.Task[None] | None = None
        self._catalog_ready = asyncio.Event()
        self._closed = False

    @property
    def projection(self) -> StateProjection | None:
        return self._state.projection

    @property
    def actions(self) -> OperationController:
        return self._actions

    @property
    def selected_participant_id(self) -> str | None:
        if self.is_running:
            try:
                return self.query_one(ParticipantTree).selected_participant_id
            except NoMatches:
                pass
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

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        """Replace Textual's redundant Keys entry with the dashboard hints."""
        for command in super().get_system_commands(screen):
            if command.title != REGIE_PALETTE_KEYS_COMMAND_TITLE:
                yield command

    async def on_mount(self) -> None:
        self._lag_task = asyncio.create_task(lag_monitor(self._lag_stopping))
        try:
            with startup_phase("presentation"):
                await self._staging.open()
        except Exception as exc:
            self.notify(f"tmux presentation unavailable: {exc}", severity="warning")
        self.query_one("#sidebar").styles.width = self.settings.sidebar_width
        self.query_one(ParticipantTree).loading = True
        if self.settings.theme and self.settings.theme in self.available_themes:
            self.theme = self.settings.theme
        elif self.settings.theme:
            available = ", ".join(sorted(self.available_themes))
            self.notify(
                f"unknown theme {self.settings.theme!r} — available: {available}",
                title="config",
                severity="warning",
                timeout=10,
            )
        window = self._cost_window()
        if window != self.settings.cost_window:
            available = ", ".join(sorted(REGIE_COST_WINDOW_LABELS))
            self.notify(
                f"unknown cost_window {self.settings.cost_window!r} — using 'day'. "
                f"available: {available}",
                title="config",
                severity="warning",
                timeout=10,
            )
        self.query_one("#usage-period", UsagePeriodBar).period_label = REGIE_COST_WINDOW_LABELS[
            window
        ]
        self._show_bus_visibility()
        self._sync_surface()
        self.call_after_refresh(self._start_initial_load)

    def _start_initial_load(self) -> None:
        if self._closed or self._startup_task is not None:
            return
        startup_milestone("first_frame", self._startup_started_at)
        self._startup_task = asyncio.create_task(self._initialize_ui(), name="regie-startup")
        self._startup_task.add_done_callback(self._initial_load_finished)

    def _initial_load_finished(self, task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._handle_exception(exc)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # An empty, still-loading tree must not send navigation into the footer.
        if self._initial_projection_pending and action.startswith("cursor_"):
            return False
        return super().check_action(action, parameters)

    async def _initialize_ui(self) -> None:
        async with asyncio.TaskGroup() as group:
            catalog = group.create_task(self._load_initial_catalog())
            group.create_task(
                start_reader(
                    lambda: self._initialize_projection(catalog=catalog),
                    interval=self.settings.tree_interval,
                    poll=self._tick_synchronize,
                    start_timer=self.set_interval,
                )
            )
            for phase, poll, interval, load in (
                ("usage", self._refresh_usage, REGIE_USAGE_POLL_INTERVAL_SECONDS, True),
                ("animations", self._refresh_animations, self.settings.bus_interval, True),
                ("bus", self._refresh_bus, self.settings.bus_interval, self._bus_visible),
            ):
                group.create_task(
                    start_reader(
                        partial(startup_stage, phase, poll) if load else None,
                        interval=interval,
                        poll=poll,
                        start_timer=self.set_interval,
                    )
                )
        self.call_after_refresh(startup_milestone, "ready", self._startup_started_at)

    async def _load_initial_catalog(self) -> bool:
        try:
            return await startup_stage("catalog", self._load_catalog)
        finally:
            self._catalog_ready.set()

    async def wait_for_catalog(self) -> None:
        """A palette may wait for startup without owning or cancelling its reads."""
        await self._catalog_ready.wait()

    async def _load_catalog(self) -> bool:
        async with self._catalog_lock:
            try:
                self._harnesses = (await self._clients.catalog.catalogs.harnesses()).value.items
            except (
                FrontendClientError,
                FrontendResponseError,
                FrontendTransportError,
                TypeError,
            ) as exc:
                logger.debug("harness catalog unavailable: %s", exc)
                try:
                    self._harnesses = local_harness_catalog()
                except Exception as fallback_exc:
                    logger.debug("local harness catalog unavailable: %s", fallback_exc)
                    return False
                self.query_one(WelcomeDashboard).show_catalog(self._harnesses)
                return False
            self.query_one(WelcomeDashboard).show_catalog(self._harnesses)
            return True

    async def _initialize_projection(self, *, catalog: asyncio.Task[bool] | None = None) -> None:
        try:
            with startup_phase("snapshot"):
                projection = await self._state.initialize()
        except (
            FrontendClientError,
            FrontendResponseError,
            FrontendTransportError,
            StateSynchronizationError,
            TypeError,
        ) as exc:
            self._show_state_error(exc)
            self._finish_initial_projection()
            return
        self._last_state_error = None
        if catalog is not None:
            await catalog
        projection = self._state.projection or projection
        with startup_phase("unmanaged"):
            await self._refresh_unmanaged(projection)
        with startup_phase("projection"):
            self._show_projection(self._state.projection or projection)
        self._render_pending_actions()

    def _finish_initial_projection(self) -> None:
        self._initial_projection_pending = False
        self.query_one(ParticipantTree).loading = False
        self.refresh_bindings()

    async def _refresh_usage(self) -> None:
        window = self._cost_window()
        try:
            usage = await self._usage.refresh(window=window)
        except (FrontendClientError, FrontendResponseError, FrontendTransportError, TypeError):
            return
        summary = dict(usage.summary)
        windowed = summary.get("windowed")
        average = summary.get("average")
        totals = dict(windowed) if isinstance(windowed, dict) else dict(usage.totals)
        self.query_one("#stats-footer", StatsFooter).totals = totals
        self.query_one("#price-footer", PriceFooter).totals = totals
        if isinstance(average, dict):
            active_days = average.get("active_days", REGIE_USAGE_AVERAGE_WINDOW_DAYS)
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
        self.query_one("#usage-period", UsagePeriodBar).period_label = REGIE_COST_WINDOW_LABELS[
            window
        ]
        self._render_usage_breakdown()

    def _cost_window(self) -> str:
        window = self.settings.cost_window
        return window if window in REGIE_COST_WINDOW_LABELS else "day"

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
            self._usage_panel.begin_first_open()
        if outcome in {ActivateOutcome.FIRST_OPEN, ActivateOutcome.SWITCH}:
            if self._usage_panel.detailed:
                self._ensure_detailed_usage_fetch()
            else:
                self._ensure_compact_usage_fetch()
        self._render_usage_breakdown()

    def _ensure_compact_usage_fetch(self) -> None:
        generation = self._usage_panel.begin_compact_fetch()
        if generation is not None:
            self.run_worker(self._fetch_compact_usage(generation), exclusive=False)

    def _ensure_detailed_usage_fetch(self) -> None:
        generation = self._usage_panel.begin_detailed_fetch()
        if generation is not None:
            self.run_worker(self._fetch_detailed_usage(generation), exclusive=False)

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
        self._usage_panel.toggle_detailed()
        self._render_usage_breakdown()
        if self._usage_panel.detailed:
            self._ensure_detailed_usage_fetch()
        else:
            self._ensure_compact_usage_fetch()

    async def _fetch_compact_usage(self, generation: int) -> None:
        try:
            result = await self._usage.breakdown()
        except (
            FrontendClientError,
            FrontendResponseError,
            FrontendTransportError,
            TypeError,
        ) as exc:
            accepted = self._usage_panel.accept_fetch(
                generation=generation,
                result=None,
                message=f"usage unavailable: {exc}",
            )
        else:
            accepted = self._usage_panel.accept_fetch(
                generation=generation, result=result, message=None
            )
        if accepted is FetchAccept.ACCEPTED and not self._usage_panel.detailed:
            self._render_usage_breakdown()

    async def _fetch_detailed_usage(self, generation: int) -> None:
        try:
            result = await self._usage.detailed_breakdown()
        except (FrontendClientError, FrontendResponseError, FrontendTransportError, TypeError):
            accepted = self._usage_panel.accept_detailed_fetch(
                generation=generation,
                result=self._usage_panel.breakdown,
                message="model details unavailable",
            )
        else:
            accepted = self._usage_panel.accept_detailed_fetch(
                generation=generation, result=result, message=None
            )
        if accepted is FetchAccept.ACCEPTED and self._usage_panel.detailed:
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
            TypeError,
        ) as exc:
            self._show_state_error(exc)
            stale_projection = self._state.projection
            if stale_projection is not None:
                self._show_projection(stale_projection)
            return
        self._last_state_error = None
        if was_stale and not projection.stale:
            await self._actions.refresh_pending()
        await self._refresh_catalog_if_dirty(projection)
        await self._refresh_unmanaged(projection)
        self._show_projection(projection)
        self._render_pending_actions()

    async def _refresh_unmanaged(self, projection: StateProjection, *, force: bool = False) -> None:
        """Refresh local-only panes independently from the public state stream."""
        checked_at = monotonic()
        if (
            not force
            and self._unmanaged_polled_at is not None
            and checked_at - self._unmanaged_polled_at < REGIE_UNMANAGED_POLL_INTERVAL_SECONDS
        ):
            return
        self._unmanaged_polled_at = checked_at
        try:
            panes = await self.presentation.unmanaged_panes(
                harness_commands={
                    entry.name: tuple(
                        command
                        for command in (entry.binary, *entry.binaries)
                        if command is not None
                    )
                    for entry in self._harnesses
                    if entry.binary
                }
            )
        except Exception as exc:
            logger.debug("unmanaged pane refresh failed: %s", exc)
            if self._unmanaged is None:
                self._unmanaged = ()
            return
        managed = {
            route.identity.terminal_id
            for participant in projection.participants.values()
            if (route := participant.terminal_route) is not None
        }
        self._unmanaged = tuple(pane for pane in panes if pane.pane_id not in managed)
        stage_result = await self._staging.reconcile()
        if stage_result is not None:
            self._show_stage_result(stage_result)

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
        except (
            AttributeError,
            FrontendClientError,
            FrontendResponseError,
            FrontendTransportError,
            TypeError,
        ) as exc:
            logger.debug("diagnostic bus unavailable: %s", exc)
            return
        view = self.query_one("#bus", RichLog)
        if self._bus.last_gap:
            view.write(Text(f"... {self._bus.last_gap} events dropped", style="dim italic"))
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
            TypeError,
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
        staged_unmanaged_id = (
            staged.terminal_id if isinstance(staged, LocalPresentationTarget) else None
        )
        tree = self.query_one(ParticipantTree)
        harness_icons = {
            harness.name: harness.icon for harness in self._harnesses if harness.icon is not None
        }
        selected = tree.show_projection(
            projection,
            participant_detail=self.settings.participant_detail,
            cwd_segments=self.settings.cwd_segments,
            stage_reasons=stage_reasons,
            harness_icons=harness_icons,
            selected_id=selected,
            staged_id=staged_id,
            staged_unmanaged_id=staged_unmanaged_id,
            trajectory_id=self._surface.trajectory_participant_id,
            unmanaged=[
                {**pane.to_tree_row(), "icon": harness_icons.get(pane.harness or "")}
                for pane in self._unmanaged or ()
            ],
        )
        self._navigation.select(selected)
        self._sync_surface()
        if self._initial_projection_pending:
            self._finish_initial_projection()
            self.call_after_refresh(
                startup_milestone, "participants_ready", self._startup_started_at
            )

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
        signature = (state, str(exc))
        if signature == self._last_state_error:
            return
        self._last_state_error = signature
        logger.warning("state %s: %s", state, exc)
        if projection is None:
            self.notify(f"state {state}: {exc}", severity="warning")

    def _sync_surface(self) -> None:
        dashboard = self.query_one("#catalog-dashboard", WelcomeDashboard)
        trajectory = self._trajectory_view()
        pane_staged = self._staging.staged_target is not None
        self.query_one("#right-surface", Vertical).set_class(pane_staged, "-pane-staged")
        dashboard.set_staged(pane_staged or self._surface.mode is SurfaceMode.TRAJECTORY)
        dashboard.display = self._surface.mode is SurfaceMode.DASHBOARD
        if trajectory is not None:
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
            staged_unmanaged_id=(
                self._staging.staged_target.terminal_id
                if isinstance(self._staging.staged_target, LocalPresentationTarget)
                else None
            ),
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
        selected = self.query_one(ParticipantTree).selected_participant_id
        return selected if projection is not None and selected in projection.participants else None

    def _selected_unmanaged_pane(self) -> str | None:
        if self._usage_panel.in_footer:
            return None
        return self.query_one(ParticipantTree).selected_unmanaged_pane

    def _trajectory_view(self) -> TrajectoryView | None:
        view = self._trajectory_view_widget
        return view if view is not None and view.is_mounted else None

    def _trajectory_has_focus(self) -> bool:
        view = self._trajectory_view()
        return view is not None and view.has_focus_within

    async def _mount_trajectory(self, participant_id: str) -> TrajectoryView:
        current = self._trajectory_view()
        if current is not None and current.participant_id == participant_id:
            return current
        if current is not None:
            await current.remove()
        view = TrajectoryView(
            participant_id,
            controller=self._trajectory,
            copy_request=self._copy_trajectory,
            focus_on_mount=False,
            id="trajectory-view",
        )
        self._trajectory_view_widget = view
        surface = self.query_one("#right-surface", Vertical)
        await surface.mount(view)
        return view

    def select_participant(self, participant_id: str) -> None:
        """Select a stable participant ID from a pointer interaction."""
        projection = self._state.projection
        if projection is None or participant_id not in projection.participants:
            return
        if self._usage_panel.in_footer:
            self._leave_usage_metrics()
        self._navigation.select(participant_id)
        self.query_one(ParticipantTree).select(participant_id)

    def select_tree_item(self, key: Key, item_id: str) -> None:
        """Select one rendered tree row without treating a local pane as a participant."""
        if self._usage_panel.in_footer:
            self._leave_usage_metrics()
        tree = self.query_one(ParticipantTree)
        tree.select_key(key)
        if key[0] == "p" and tree.selected_participant_id == item_id:
            self._navigation.select(item_id)

    def _move_selection(self, offset: int) -> None:
        tree = self.query_one(ParticipantTree)
        tree.move(offset)
        if tree.selected_participant_id is not None:
            self._navigation.select(tree.selected_participant_id)

    def action_cursor_down(self) -> None:
        metric = self._usage_panel.keyboard_metric
        if metric is not None:
            target = REGIE_USAGE_METRIC_DOWN.get(metric)
            if target is not None:
                self._select_usage_metric(target, origin=metric)
            return
        tree = self.query_one(ParticipantTree)
        keys = tree.selectable_keys
        if keys and tree.selected_key != keys[-1]:
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
        if self._trajectory_has_focus():
            return
        participant_id = self._selected_id()
        if participant_id is None:
            message = (
                "adopt this pane before opening its trajectory"
                if self._selected_unmanaged_pane() is not None
                else "nothing to inspect"
            )
            self.notify(message, severity="warning")
            return
        if (
            self._surface.mode is SurfaceMode.TRAJECTORY
            and self._surface.trajectory_participant_id == participant_id
        ):
            view = self._trajectory_view()
            if view is not None:
                view.focus_region(view.state.focus_region)
            return
        self._trajectory_navigation.clear()
        if await self.open_trajectory(participant_id) is not None:
            self.set_focus(None)

    async def action_cursor_right_or_focus(self) -> None:
        if self._usage_panel.in_footer:
            self.action_cursor_right()
            return
        if self._trajectory_has_focus():
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
        result: StageResult | None
        participant_id = self._selected_id()
        if participant_id is None:
            unmanaged = self._selected_unmanaged_pane()
            if unmanaged is None:
                self.notify("nothing to stage", severity="warning")
                return
            result = await self._staging.stage_unmanaged(unmanaged)
        else:
            result = await self.stage_participant(participant_id)
        self._show_stage_result(result)

    async def action_focus_stage(self) -> None:
        if not self._selection_is_staged():
            participant_id = self._selected_id()
            if participant_id is None:
                unmanaged = self._selected_unmanaged_pane()
                if unmanaged is None:
                    self.notify("nothing to stage", severity="warning")
                    return
                self._show_stage_result(await self._staging.stage_unmanaged(unmanaged))
            else:
                self._show_stage_result(await self.stage_participant(participant_id))
            return
        result = await self._staging.focus()
        if result.outcome is StageOutcome.FOCUSED:
            self._set_status("staged terminal focused")
        else:
            self.notify(result.reason or "no terminal is staged", severity="warning")

    async def action_stage_and_focus_tmux(self) -> None:
        if not self._selection_is_staged():
            result: StageResult | None
            participant_id = self._selected_id()
            if participant_id is None:
                unmanaged = self._selected_unmanaged_pane()
                if unmanaged is None:
                    self.notify("nothing to stage", severity="warning")
                    return
                result = await self._staging.stage_unmanaged(unmanaged)
            else:
                result = await self.stage_participant(participant_id)
            self._show_stage_result(result)
            if result is None or result.outcome is not StageOutcome.STAGED:
                return
        await self.action_focus_stage()

    def _selection_is_staged(self) -> bool:
        projection = self._state.projection
        participant_id = self._selected_id()
        if participant_id is None:
            pane_id = self._selected_unmanaged_pane()
            target = self._staging.staged_target
            return (
                pane_id is not None
                and isinstance(target, LocalPresentationTarget)
                and target.terminal_id == pane_id
            )
        if projection is None:
            return False
        participant = projection.participants.get(participant_id)
        if participant is None:
            return False
        target = stageability(participant, projection.providers, self.presentation).target
        return target is not None and target == self._staging.staged_target

    def _show_stage_result(self, result: StageResult | None) -> None:
        if result is None:
            return
        if result.outcome is StageOutcome.STAGED:
            self._surface.show_dashboard()
        if result.outcome in {StageOutcome.STAGED, StageOutcome.UNSTAGED}:
            self._set_status(result.outcome.value)
        elif result.outcome is StageOutcome.UNSTAGEABLE:
            self.notify(result.reason or "terminal cannot be staged", severity="warning")
        elif result.outcome is StageOutcome.FAILED:
            self.notify(result.reason or "stage failed: unknown error", severity="error")
        elif result.outcome is not StageOutcome.FOCUSED:
            self.notify(result.reason or "stage unavailable", severity="warning")
        self._sync_surface()

    async def open_trajectory(self, participant_id: str) -> TrajectoryView | None:
        if self._staging.staged_target is not None:
            result = await self._staging.unstage()
            self._show_stage_result(result)
            if result.outcome is not StageOutcome.UNSTAGED:
                return None
        view = await self._mount_trajectory(participant_id)
        self._surface.show_trajectory(participant_id)
        view.enter_live_tail()
        self._sync_surface()
        return view

    async def action_toggle_trajectory(self) -> None:
        participant_id = self._selected_id()
        if participant_id is None:
            message = (
                "adopt this pane before opening its trajectory"
                if self._selected_unmanaged_pane() is not None
                else "nothing to inspect"
            )
            self.notify(message, severity="warning")
            return
        if (
            self._surface.mode is SurfaceMode.TRAJECTORY
            and self._surface.trajectory_participant_id == participant_id
        ):
            self._surface.show_dashboard()
            self._sync_surface()
            return
        self._trajectory_navigation.clear()
        if await self.open_trajectory(participant_id) is not None:
            self.set_focus(None)

    async def action_stage_and_focus_trajectory(self) -> None:
        participant_id = self._selected_id()
        if participant_id is None:
            message = (
                "adopt this pane before opening its trajectory"
                if self._selected_unmanaged_pane() is not None
                else "nothing to inspect"
            )
            self.notify(message, severity="warning")
            return
        self._trajectory_navigation.clear()
        view = await self.open_trajectory(participant_id)
        if view is not None:
            view.focus_region(view.state.focus_region)

    async def action_trajectory_previous(self) -> None:
        view = self._trajectory_view()
        if view is not None and self._surface.mode is SurfaceMode.TRAJECTORY:
            view.action_previous_page()

    async def action_trajectory_next(self) -> None:
        view = self._trajectory_view()
        if view is not None and self._surface.mode is SurfaceMode.TRAJECTORY:
            view.action_next_page()

    def action_trajectory_search(self) -> None:
        view = self._trajectory_view()
        if view is not None and self._surface.mode is SurfaceMode.TRAJECTORY:
            view.action_open_search()

    async def action_return_to_tree(self) -> None:
        self.set_focus(None)
        self.query_one(ParticipantTree).set_cursor_visible(True)

    def on_return_to_tree(self, _message: ReturnToTree) -> None:
        self.set_focus(None)
        self.query_one(ParticipantTree).set_cursor_visible(True)

    async def _copy_trajectory(self, text: str) -> None:
        try:
            await self.presentation.copy_text(text)
        except Exception as exc:
            self.notify(f"copy failed: {exc}", severity="error")
        else:
            self.notify("copied")

    def _trajectory_origin(self) -> tuple[str, str] | None:
        view = self._trajectory_view()
        if view is None:
            return None
        record_id = view.state.row_anchor(view.state.selected_id)
        return (view.participant_id, record_id) if record_id is not None else None

    async def on_trajectory_participant_selected(
        self, message: TrajectoryParticipantSelected
    ) -> None:
        origin = self._trajectory_origin()
        if (
            await self._navigate_trajectory_link(message.participant_id, message.target_record_id)
            and origin is not None
        ):
            self._trajectory_navigation.push(*origin)

    async def on_trajectory_back_requested(self, _message: TrajectoryBackRequested) -> None:
        target = self._trajectory_navigation.back()
        if target is None:
            return
        if not await self._navigate_trajectory_link(target.participant_id, target.record_id):
            self._trajectory_navigation.push(target.participant_id, target.record_id)

    async def _navigate_trajectory_link(self, participant_id: str, record_id: str | None) -> bool:
        projection = self._state.projection
        if projection is None or participant_id not in projection.participants:
            self.notify("linked participant is no longer in the tree", severity="warning")
            return False
        self.select_participant(participant_id)
        view = await self.open_trajectory(participant_id)
        if view is None:
            return False
        view.focus_region(view.state.focus_region)
        if record_id is not None:
            await self._reveal_trajectory_target(view, record_id)
        return True

    async def _reveal_trajectory_target(self, view: TrajectoryView, record_id: str) -> None:
        await view.wait_until_loaded()
        if view.select_and_reveal_record(record_id):
            return
        try:
            location = await self._trajectory.locate(view.participant_id, record_id)
        except Exception as exc:
            self.notify(f"linked event lookup failed: {exc}", severity="warning")
            return
        if location.resolution is not TrajectoryLocationResolution.EXACT or location.record is None:
            self.notify(location.message or "linked event is unavailable", severity="warning")
            return
        view.state.upsert((location.record,))
        if not view.select_and_reveal_record(record_id):
            self.notify("linked event could not be shown", severity="warning")

    async def on_trajectory_retry_requested(self, message: TrajectoryRetryRequested) -> None:
        await self._trajectory.retry(message.participant_id)

    async def on_trajectory_copy_requested(self, message: TrajectoryCopyRequested) -> None:
        await self._copy_trajectory(message.text)

    def action_toggle_bus(self) -> None:
        self._bus_visible = not self._bus_visible
        self._show_bus_visibility()

    def action_recover_transcript(self) -> None:
        participant_id = self._selected_id()
        if participant_id is None:
            message = (
                "adopt this pane before recovering transcript identity"
                if self._selected_unmanaged_pane() is not None
                else "no participant selected"
            )
            self.notify(message, severity="warning")
            return
        if not self.transcript_recovery_available(participant_id):
            self.notify("transcript identity is already trusted", severity="information")
            return
        self._transcript_recovery_target = participant_id
        self.push_screen(
            CommandPalette(
                providers=[TranscriptCandidateCommands],
                placeholder="Choose a transcript candidate…",
            ),
            self._transcript_palette_closed,
        )

    def transcript_recovery_available(self, participant_id: str) -> bool:
        projection = self._state.projection
        participant = None if projection is None else projection.participants.get(participant_id)
        identity = None if participant is None else participant.transcript_identity
        return participant is not None and (identity is None or identity.state != "trusted")

    def _transcript_palette_closed(self, _result: object = None) -> None:
        self._transcript_recovery_target = None
        self._restore_tree_focus()

    async def load_transcript_candidates(self) -> tuple[TranscriptCandidate, ...]:
        participant_id = self._transcript_recovery_target
        if participant_id is None:
            return ()
        async with self._transcript_candidates_lock:
            try:
                page = await self._clients.transcripts.transcripts.candidates(participant_id)
            except (
                FrontendClientError,
                FrontendResponseError,
                FrontendTransportError,
                TypeError,
            ) as exc:
                self.notify(f"transcript candidates unavailable: {exc}", severity="warning")
                return ()
        candidates = tuple(
            candidate for candidate in page.value.items if candidate.rejection_reason is None
        )
        if not candidates:
            self.notify("no bindable transcript candidates were found", severity="warning")
        return candidates

    def select_transcript_candidate(
        self,
        candidate: TranscriptCandidate,
        *,
        participant_id: str | None = None,
    ) -> None:
        participant_id = participant_id or self._transcript_recovery_target
        projection = self._state.projection
        if (
            participant_id is None
            or projection is None
            or participant_id not in projection.participants
        ):
            self.notify("the transcript recovery target is no longer available", severity="warning")
            return
        if candidate.rejection_reason:
            self.notify(
                f"candidate cannot be bound: {candidate.rejection_reason}",
                severity="warning",
            )
            return
        owners = {
            owner
            for owner in (candidate.owner_id, candidate.tombstone_id)
            if owner is not None and owner != participant_id
        }
        if len(owners) > 1:
            self.notify("candidate has conflicting ownership metadata", severity="error")
            return
        prior_owner_id = next(iter(owners), None)
        if prior_owner_id is None:
            self._start_transcript_bind(participant_id, candidate, prior_owner_id=None)
            self._transcript_recovery_target = None
            return

        def receive(confirmed_owner_id: str | None) -> None:
            if confirmed_owner_id == prior_owner_id:
                self._start_transcript_bind(
                    participant_id,
                    candidate,
                    prior_owner_id=prior_owner_id,
                )
            self._transcript_recovery_target = None
            self._restore_tree_focus()

        self.push_screen(
            TranscriptTransferScreen(
                location=candidate.location,
                prior_owner_id=prior_owner_id,
                owner_is_dead=candidate.tombstone_id == prior_owner_id,
            ),
            receive,
        )

    def _start_transcript_bind(
        self,
        participant_id: str,
        candidate: TranscriptCandidate,
        *,
        prior_owner_id: str | None,
    ) -> None:
        self.run_worker(
            self._bind_transcript_candidate(
                participant_id,
                candidate.location,
                prior_owner_id=prior_owner_id,
            ),
            exclusive=False,
        )

    async def _bind_transcript_candidate(
        self,
        participant_id: str,
        location: str,
        *,
        prior_owner_id: str | None,
    ) -> None:
        record = await self._transcript_bindings.bind(
            participant_id,
            location,
            prior_owner_id=prior_owner_id,
        )
        if record.state is TranscriptBindState.PENDING:
            self.notify("transcript bind is already in progress", severity="information")
            return
        if record.state is TranscriptBindState.UNCERTAIN:
            self.notify(
                "transcript bind outcome is uncertain; choose the same candidate to retry safely",
                severity="warning",
            )
            return
        if record.state is TranscriptBindState.REFUSED:
            self.notify(f"transcript bind refused: {record.detail}", severity="error")
            return
        self.notify("transcript identity recovered", severity="information")
        try:
            projection = await self._state.initialize()
        except (
            FrontendClientError,
            FrontendResponseError,
            FrontendTransportError,
            StateSynchronizationError,
            TypeError,
        ) as exc:
            self._show_state_error(exc)
            return
        self._last_state_error = None
        await self._refresh_unmanaged(projection, force=True)
        self._show_projection(projection)
        if self._surface.trajectory_participant_id == participant_id:
            try:
                await self._trajectory.retry(participant_id)
            except Exception as exc:
                self.notify(f"trajectory refresh failed: {exc}", severity="warning")
        self._restore_tree_focus()

    def _restore_tree_focus(self) -> None:
        if not self.is_running:
            return
        self.set_focus(None)
        self.query_one(ParticipantTree).set_cursor_visible(True)

    def action_resume_palette(self) -> None:
        self.push_screen(
            CommandPalette(
                providers=[ResumeSessionCommands],
                placeholder="Search for dead sessions…",
            )
        )

    async def load_resume_sessions(self) -> ResumeDiscovery:
        """Load palette candidates on its isolated ordinary-request connection."""
        async with self._resume_discovery_lock:
            return await discover_resume_sessions(self._clients.resume)

    def _spawn_choices(self) -> tuple[SpawnChoice, ...]:
        return spawn_choices(self._harnesses, self.settings.favourite)

    def icon_for_harness(self, harness: str) -> str | None:
        """Return the daemon-advertised icon for one canonical harness."""
        return next((entry.icon for entry in self._harnesses if entry.name == harness), None)

    def action_spawn(self) -> None:
        self.push_screen(
            CommandPalette(
                providers=[SpawnHarnessCommands],
                placeholder="Spawn a fresh session…",
            )
        )

    def spawn_harness(self, harness: str) -> None:
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
            discovery = await self.load_resume_sessions()
        except (
            FrontendClientError,
            FrontendResponseError,
            FrontendTransportError,
            TypeError,
        ) as exc:
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
        approval = self._spawn_approval(candidate.harness)
        if approval is not None:
            self._start_action(self.submit_resume(candidate, "", approval))

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
            if values is None:
                return
            if not any(values):
                self.notify("give a model or a reasoning effort", severity="warning")
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
        async with self._controls_inspection_lock:
            try:
                controls = (await self._clients.controls.controls.get(participant_id)).value
            except (
                FrontendClientError,
                FrontendResponseError,
                FrontendTransportError,
                TypeError,
            ) as exc:
                self.notify(f"controls unavailable: {exc}", severity="warning")
                return
        self.notify(
            format_controls_report(controls),
            title="Session controls",
            timeout=REGIE_CONTROLS_REPORT_TIMEOUT_SECONDS,
            markup=False,
        )

    def action_kill(self) -> None:
        if self._usage_panel.in_footer:
            return
        participant_id = self._selected_id()
        if participant_id is None:
            message = (
                "adopt this pane before terminating it"
                if self._selected_unmanaged_pane() is not None
                else "nothing to terminate"
            )
            self.notify(message, severity="warning")
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

    def _action_changed(self, record: ActionRecord) -> None:
        self.call_later(self._render_pending_actions)

    def _render_pending_actions(self) -> None:
        for record in self._actions.records:
            signature = self._action_signature(record)
            changed = signature != self._action_signatures.get(record.idempotency_key)
            if changed or self._action_needs_reconciliation(record):
                self._show_action(record, announce=changed)

    @staticmethod
    def _action_signature(record: ActionRecord) -> tuple[object, ...]:
        return (
            record.action,
            record.target_id,
            record.state,
            record.phase,
            record.job_handle,
            record.detail,
            repr(record.result),
        )

    def _show_action(self, record: ActionRecord, *, announce: bool = True) -> None:
        message, severity = describe_action(record)
        self._set_status(message)
        signature = self._action_signature(record)
        changed = signature != self._action_signatures.get(record.idempotency_key)
        self._last_action_signature = signature
        self._action_signatures[record.idempotency_key] = signature
        if announce and changed and severity in {"warning", "error"}:
            self.notify(message, severity=severity)
        if self._action_needs_reconciliation(record):
            self._reconciling_actions.add(record.idempotency_key)
            self.run_worker(self._reconcile_completed_action(record), exclusive=False)

    def _action_needs_reconciliation(self, record: ActionRecord) -> bool:
        return (
            record.state.value == "succeeded"
            and record.action in {"spawn", "resume", "terminate"}
            and record.idempotency_key not in self._reconciled_actions
            and record.idempotency_key not in self._reconciling_actions
        )

    async def _reconcile_completed_action(self, record: ActionRecord) -> None:
        try:
            participant_id = record.participant_id
            if record.action == "terminate" and participant_id is not None:
                self.query_one(ParticipantTree).remove_without_animation(participant_id)
                projection = self._state.projection
                if (
                    projection is not None
                    and self._participant_id_for_target(self._staging.staged_target, projection)
                    == participant_id
                ):
                    self._show_stage_result(await self._staging.unstage())
                if self._surface.trajectory_participant_id == participant_id:
                    self._surface.show_dashboard()
                    self._sync_surface()
            try:
                with action_phase(record, "snapshot"):
                    projection = await self._state.initialize()
            except (
                FrontendClientError,
                FrontendResponseError,
                FrontendTransportError,
                StateSynchronizationError,
                TypeError,
            ) as exc:
                self._show_state_error(exc)
                return
            self._last_state_error = None
            with action_phase(record, "unmanaged"):
                await self._refresh_unmanaged(projection, force=True)
            with action_phase(record, "projection"):
                self._show_projection(projection)
                self.set_focus(None)
                self.query_one(ParticipantTree).set_cursor_visible(True)
            self._reconciled_actions.add(record.idempotency_key)
            self.call_after_refresh(self._record_action_rendered, record, monotonic())
        finally:
            self._reconciling_actions.discard(record.idempotency_key)

    def _record_action_rendered(self, record: ActionRecord, projected_at: float) -> None:
        displayed_at = monotonic()
        logger.info(
            "action.%s.rendered %.1fms operation=%s after_observation_ms=%s "
            "after_projection_ms=%.1f",
            record.action,
            (displayed_at - record.submitted_at) * 1000,
            record.operation_id,
            None
            if record.observed_at is None
            else round((displayed_at - record.observed_at) * 1000, 1),
            (displayed_at - projected_at) * 1000,
        )

    def _set_status(self, message: str) -> None:
        logger.debug("Régie status: %s", message)

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
        projection = self._state.projection
        participant = None if projection is None else projection.participants.get(participant_id)
        if participant is not None:
            result = await self._staging.unstage_participant(participant)
            if result is not None:
                if result.outcome is StageOutcome.FAILED:
                    return self._actions.refuse_locally(
                        "terminate",
                        participant_id,
                        result.reason or "could not release staged pane",
                    )
                self._show_stage_result(result)
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

    def latest_uncertain_action(self) -> ActionRecord | None:
        return next(
            (
                record
                for record in reversed(self._actions.records)
                if record.state.value == "uncertain"
            ),
            None,
        )

    def retry_latest_action(self) -> None:
        record = self.latest_uncertain_action()
        if record is not None:
            self._start_action(self.retry_action(record.action, record.target_id))

    async def action_quit(self) -> None:
        """Restore local presentation before exit; this never terminates a terminal."""
        if not self._closed:
            self._closed = True
            await self._cancel_startup()
            with contextlib.suppress(Exception):
                await self._staging.close()
        self.exit()

    async def _cancel_startup(self) -> None:
        task = self._startup_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._catalog_ready.set()

    async def on_unmount(self) -> None:
        restore_presentation = not self._closed
        self._closed = True
        await self._cancel_startup()
        self._lag_stopping.set()
        self._stop_animation_timer()
        await self._actions.close()
        await self._transcript_bindings.close()
        with contextlib.suppress(Exception):
            await self._trajectory.close()
        if restore_presentation:
            with contextlib.suppress(Exception):
                await self._staging.close()
        with contextlib.suppress(Exception):
            await self._clients.close()
        if self._lag_task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._lag_task
            self._lag_task = None

    def _handle_exception(self, error: Exception) -> None:
        log_exception(logger, "Régie crashed", error)
        super()._handle_exception(error)


def catalog_names(harnesses: Iterable[HarnessCatalogEntry]) -> tuple[str, ...]:
    """Expose public catalog names for palette adapters without local discovery."""
    return tuple(entry.name for entry in harnesses if entry.launch_available)


__all__ = ["RegieApp", "catalog_names"]
