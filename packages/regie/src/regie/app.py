"""The independent, public-SDK Régie Textual application."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterable
from functools import partial
from time import monotonic
from typing import TYPE_CHECKING, ClassVar

from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import RichLog

from regie.animations.routes import RouteAnimationController
from regie.app_parts import (
    ActionTracking,
    ControlActions,
    DiagnosticsDisplay,
    ProjectionSync,
    SpawnResume,
    StagingActions,
    StartupLoading,
    TrajectoryActions,
    TranscriptRecovery,
    TreeNavigation,
    UsageFooter,
)
from regie.app_parts._shared import logger
from regie.bus import DiagnosticBusController
from regie.client_pool import FrontendClientPool
from regie.contracts import (
    PresentationOperations,
    RegieSettings,
    UnmanagedPane,
)
from regie.controllers.action_presentation import ActionPresentation
from regie.controllers.actions import OperationController
from regie.controllers.navigation import NavigationState
from regie.controllers.polling import RefreshGate
from regie.controllers.presentation_queue import PresentationQueue
from regie.controllers.staging import StageController
from regie.controllers.state_follow import StateFollowLoop
from regie.controllers.surface import SurfaceController
from regie.controllers.transcripts import (
    TranscriptBindingController,
)
from regie.controllers.usage import UsagePanelState
from regie.dashboard import WelcomeDashboard
from regie.latency import startup_phase
from regie.observability import lag_monitor, log_exception
from regie.palette import (
    ResumeSessionCommand,
    RetryActionCommand,
    SpawnCommand,
    TranscriptRecoveryCommand,
    UsageViewCommand,
    ViewCommands,
)
from regie.resume import ResumeCandidate
from regie.state import StateController
from regie.trajectory.adapter import TrajectoryFollowAdapter, TrajectoryQueryAdapter
from regie.trajectory.rich import (
    TrajectoryController,
    TrajectoryNavigationHistory,
    TrajectoryStateStore,
)
from regie.trajectory.ui_constants import TOOLTIP_DELAY
from regie.ui_constants import (
    REGIE_COST_WINDOW_LABELS,
    REGIE_PALETTE_KEYS_COMMAND_TITLE,
    REGIE_RETURN_SIGNAL_TEXTUAL,
)
from regie.usage import UsageController
from regie.widgets import (
    ParticipantTree,
    PriceFooter,
    StatsFooter,
    TreeStack,
    UsageBreakdownPanel,
    UsagePeriodBar,
)
from theater.frontend import (
    FrontendClient,
    StateProjection,
)
from theater.frontend.dto.catalogs import HarnessCatalogEntry

if TYPE_CHECKING:
    from regie.trajectory.rich.view import TrajectoryView


class RegieApp(
    StartupLoading,
    UsageFooter,
    ProjectionSync,
    DiagnosticsDisplay,
    TreeNavigation,
    StagingActions,
    TrajectoryActions,
    TranscriptRecovery,
    SpawnResume,
    ControlActions,
    ActionTracking,
    App[None],
):
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
        Binding("h", "request_trajectory('left')", "trajectory", show=False),
        Binding("left", "cursor_left", "left", show=False),
        Binding("l", "request_presentation('focus')", "focus", show=False),
        Binding("right", "cursor_right", "right", show=False),
        Binding("enter", "request_presentation('toggle')", "stage"),
        Binding(REGIE_RETURN_SIGNAL_TEXTUAL, "return_to_tree", show=False, priority=True),
        Binding("H,shift+h", "request_trajectory('open')", "open trajectory", show=False),
        Binding("L,shift+l", "request_presentation('open')", "open agent", show=False),
        Binding("s", "send", "send", show=False),
        Binding("i", "interrupt_session", "interrupt", show=False),
        Binding("f", "queue_followup", "followup", show=False),
        Binding("g", "update_session_settings", "settings", show=False),
        Binding("o", "spawn", "spawn"),
        Binding("r", "resume_sessions", "resume", show=False),
        Binding("v", "toggle_bus", "bus", show=False),
        Binding("dollar_sign", "toggle_usage", "usage", show=False),
        Binding("x", "kill", "kill"),
        Binding("ctrl+p", "command_palette", "palette", show=False),
        Binding("q", "quit", "quit"),
    ]

    COMMANDS = App.COMMANDS | {
        ResumeSessionCommand,
        RetryActionCommand,
        SpawnCommand,
        TranscriptRecoveryCommand,
        UsageViewCommand,
        ViewCommands,
    }

    title = "theater régie"

    def __init__(
        self,
        *,
        client: FrontendClient,
        settings: RegieSettings,
        presentation: PresentationOperations,
        startup_started_at: float | None = None,
    ) -> None:
        self._startup_started_at = monotonic() if startup_started_at is None else startup_started_at
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
        self._presentation_queue = PresentationQueue()
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
        self._state_follow = StateFollowLoop(
            partial(self._synchronize_projection, following=True),
            retry_delay=settings.tree_interval,
            on_error=self._handle_exception,
        )
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
        self._usage_visible = settings.usage_visible
        self._last_state_error: tuple[str, str] | None = None
        self._action_presentation = ActionPresentation()
        self._lag_stopping = asyncio.Event()
        self._lag_task: asyncio.Task[None] | None = None
        self._startup_task: asyncio.Task[None] | None = None
        self._catalog_ready = asyncio.Event()
        self._projection_ready = asyncio.Event()
        self._closed = False

    @property
    def projection(self) -> StateProjection | None:
        return self._state.projection

    @property
    def _view_active(self) -> bool:
        # Textual stops the message pump before removing widgets and emitting Unmount.
        return self.is_running and not self._closed

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

    @property
    def usage_visible(self) -> bool:
        return self._usage_visible

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
        self._show_usage_visibility()
        self._sync_surface()
        self.call_after_refresh(self._start_initial_load)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # An empty, still-loading tree must not send navigation into the footer.
        if self._initial_projection_pending and action.startswith("cursor_"):
            return False
        return super().check_action(action, parameters)

    async def action_quit(self) -> None:
        """Restore local presentation before exit; this never terminates a terminal."""
        if not self._closed:
            self._closed = True
            await self._presentation_queue.close()
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
        await self._presentation_queue.close()
        await self._cancel_startup()
        await self._state_follow.close()
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
