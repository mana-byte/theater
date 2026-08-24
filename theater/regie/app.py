"""The régie Textual application.

Layout:
    ┌──────────────────────┬──────────────────────────┐
    │ tree (top)           │  dashboard / trajectory  │
    │                      │  replaced by tmux stage  │
    │ bus (bottom)         │                          │
    └──────────────────────┴──────────────────────────┘

The tree and bus are Textual widgets. The right side is a welcome dashboard
showing an animated sentence and rotating usage tips while no participant is staged.
When the user selects an agent, the daemon tells tmux to join that agent's pane
into the stage area, covering the dashboard; the régie then re-asserts its own
pane layout so the tree stays visible. Unstaging restores the dashboard.

Keybindings:
    j/k or up/down  navigate the tree and usage footer
    h/l              stage trajectory/live pane, then focus on repeat
    H/L              previous/next trajectory ledger page
    left/right       navigate usage footer rows
    Enter           stage the selected agent in the tree; toggle detailed usage in the footer
    Esc             return from trajectory to the tree
    <prefix> h      return to the tree from the stage or trajectory (claimed only if free)
    x               kill the selected agent's pane
    ctrl+p          command palette, including `Spawn <harness>`
    q               quit (unstages first; detaches, kills nothing)

Polling: the tree refreshes every 1s, the bus tail every 0.4s. Both are
async daemon calls; the app runs them as background workers. The bus is read
twice over, on two cursors: once for the panel, which must not consume rows
while it is hidden, and once for tree-route animation, which must run whether
the panel is showing or not.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.content import Content
from textual.dom import DOMNode
from textual.reactive import reactive
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import RichLog, Static  # noqa: F401 — tests query app_mod.Static

from theater.client import DaemonClient
from theater.config import Config, RegieSection
from theater.constants import (
    MICROCENTS_PER_DOLLAR,
    USAGE_AVERAGE_WINDOW_DAYS,
)
from theater.constants.regie import (
    REGIE_AWAIT_ANIM_TTL,
    REGIE_COST_WINDOW_HOURS,
    REGIE_COST_WINDOW_LABELS,
    REGIE_COST_WINDOW_ROLLING_LABELS,
    REGIE_EMPTY_TREE_HINT,
    REGIE_EMPTY_TREE_SHORTCUT,
    REGIE_EMPTY_TREE_SHORTCUT_STYLE,
    REGIE_EMPTY_TREE_TAIL,
    REGIE_HIDDEN_TREE_CURSOR,
    REGIE_MAX_AWAIT_ANIMS,
    REGIE_MAX_TRACE_ANIMS,
    REGIE_PALETTE_KEYS_COMMAND_TITLE,
    REGIE_RETURN_SIGNAL_TEXTUAL,
    REGIE_STARTUP_REVEAL_INTERVAL_SECONDS,
    REGIE_TRACE_ANIM_INTERVAL,
    REGIE_USAGE_METRIC_DOWN,
    REGIE_USAGE_METRIC_LEFT,
    REGIE_USAGE_METRIC_RIGHT,
    REGIE_USAGE_METRIC_UP,
    REGIE_USAGE_POLL_INTERVAL_SECONDS,
)
from theater.constants.trajectory import TRAJECTORY_TOOLTIP_DELAY_MS
from theater.regie.animations.footer import (  # noqa: F401
    _advance_float,
    _advance_int,
    _pulsing_value,
)
from theater.regie.animations.retirement import LeafRetirementController, LeafRetirementFrame
from theater.regie.animations.reveal import LeafRevealController
from theater.regie.animations.routes import (  # noqa: F401
    _AWAIT_TRACE_GLYPHS,
    _RAIL_ARMS,
    _SEND_TRACE_GLYPHS,
    AwaitRouteAnim,
    LeafOverlay,
    RouteAnim,
    RouteAnimationController,
    _await_route_glyph,
    _await_route_style,
    _send_trace_glyph,
)
from theater.regie.bus_view import format_bus_line
from theater.regie.controllers.navigation import NavigationState, UpDecision
from theater.regie.controllers.polling import PollingController
from theater.regie.controllers.session import (
    _RETURN_KEY_NOTE,  # noqa: F401 — legacy alias
    SessionController,
)
from theater.regie.controllers.staging import (
    StageController,
    StageOutcome,
    StageResult,
)
from theater.regie.controllers.surface import (
    RightSurface,
    SurfaceController,
    TrajectoryStageOutcome,
    TrajectoryStageResult,
)
from theater.regie.controllers.usage import (
    ActivateOutcome,
    FetchAccept,
    SyncOutcome,
    UsagePanelState,
    UsageQueries,
)
from theater.regie.dashboard.widgets import WelcomeDashboard
from theater.regie.palette import (
    ResumeDeadSessionCommand,
    ResumeDeadSessionCommands,
    SpawnCommand,
    SpawnHarnessCommands,
    ViewCommands,
)
from theater.regie.trajectory import (
    ReturnToTree,
    TrajectoryController,
    TrajectoryParticipantSelected,
    TrajectoryStateStore,
    TrajectoryView,
)
from theater.regie.tree import (  # noqa: F401
    DOWN,
    LEFT,
    RIGHT,
    UP,
    AwaitCell,
    Cell,
    Direction,
    Key,
    LeafCell,
    OverlayGlyph,
    await_highlight_cells,
    await_path,
    cell_leaf,
    is_root_prefix,
    node_label,
    render_tree,
    selected_participant,
    send_path,
    working_harness_style,
)
from theater.regie.widgets.chrome import (  # noqa: F401
    EmptyTreeState,
    NonSelectableStatic,
)
from theater.regie.widgets.leaf import AgentLeaf  # noqa: F401
from theater.regie.widgets.tree import (  # noqa: F401
    TreePanel,
    TreeStack,
    _is_participant_key,
)
from theater.regie.widgets.usage_breakdown import (
    UsageBreakdownPanel,
)
from theater.regie.widgets.usage_footer import (  # noqa: F401
    FOOTER_ANIM_DURATION,
    FOOTER_ANIM_FRAMES,
    FOOTER_ANIM_INTERVAL,
    PriceFooter,
    StatsFooter,
    UsageMetricTile,
    UsagePeriodBar,
    _fmt_tokens,
)
from theater.tmux import client as tmux
from theater.tmux import panes

logger = logging.getLogger("theater.regie")

#: Fallbacks; RegieApp overrides from loaded config at start-up.
_DEFAULTS = RegieSection()

TREE_INTERVAL = _DEFAULTS.tree_interval
BUS_INTERVAL = _DEFAULTS.bus_interval
BUS_BATCH = _DEFAULTS.bus_batch
USAGE_INTERVAL = REGIE_USAGE_POLL_INTERVAL_SECONDS
HIDDEN_TREE_CURSOR = REGIE_HIDDEN_TREE_CURSOR
_USAGE_METRIC_LEFT = REGIE_USAGE_METRIC_LEFT
_USAGE_METRIC_RIGHT = REGIE_USAGE_METRIC_RIGHT
_USAGE_METRIC_DOWN = REGIE_USAGE_METRIC_DOWN
_USAGE_METRIC_UP = REGIE_USAGE_METRIC_UP

#: Maps [regie] cost_window values to legacy rolling hours for older daemons.
_COST_WINDOWS = REGIE_COST_WINDOW_HOURS
_COST_WINDOW_LABELS = REGIE_COST_WINDOW_LABELS
_COST_WINDOW_ROLLING_LABELS = REGIE_COST_WINDOW_ROLLING_LABELS

EMPTY_TREE_SHORTCUT = REGIE_EMPTY_TREE_SHORTCUT
EMPTY_TREE_SHORTCUT_STYLE = REGIE_EMPTY_TREE_SHORTCUT_STYLE
EMPTY_TREE_TAIL = REGIE_EMPTY_TREE_TAIL
EMPTY_TREE_HINT = REGIE_EMPTY_TREE_HINT

TRACE_ANIM_INTERVAL = REGIE_TRACE_ANIM_INTERVAL
MAX_TRACE_ANIMS = REGIE_MAX_TRACE_ANIMS
MAX_AWAIT_ANIMS = REGIE_MAX_AWAIT_ANIMS
AWAIT_ANIM_TTL = REGIE_AWAIT_ANIM_TTL
STARTUP_REVEAL_INTERVAL = REGIE_STARTUP_REVEAL_INTERVAL_SECONDS


class RegieApp(App):
    """The theater control panel."""

    TOOLTIP_DELAY = TRAJECTORY_TOOLTIP_DELAY_MS / 1_000

    CSS = """
    Screen {
        layout: horizontal;
    }
    #sidebar {
        layout: vertical;
    }
    #right-surface {
        width: 1fr;
        height: 1fr;
        min-width: 0;
        min-height: 0;
    }
    #right-surface.-pane-staged {
        display: none;
    }
    TrajectoryView.-hidden-surface {
        display: none;
    }
    #tree-stack {
        height: 1fr;
        layers: base overlay;
    }
    #tree-panel {
        height: 1fr;
        layer: base;
    }
    #bus-panel {
        height: 18;
        padding: 1 2;
        scrollbar-size: 0 0;
    }
    #bus-panel.-hidden {
        display: none;
    }
    /* The zebra stripe and its row states. These live in App.CSS, which
       outranks AgentLeaf.DEFAULT_CSS whatever the specificity, so every
       state an alt row can be in has to be restated here or the widget's
       own cursor rule never applies to every other row. The stripe is a
       3% ink wash: $foreground darkens light themes and lightens dark
       ones, and 3% keeps it below the hover tint on all 21 themes. */
    AgentLeaf.tree-alt {
        background: $foreground 3%;
    }
    AgentLeaf.tree-alt:hover {
        background: $accent 10%;
    }
    AgentLeaf.tree-alt.tree-staged {
        background: $primary 20%;
    }
    AgentLeaf.tree-alt.tree-staged:hover {
        background: $primary 20%;
    }
    AgentLeaf.tree-alt.tree-cursor {
        background: $accent 20%;
        text-style: bold;
    }
    AgentLeaf.tree-alt.tree-cursor.tree-staged {
        background: $accent 30%;
        text-style: bold;
    }
    .log {
        background: $surface;
    }
    PriceFooter {
        height: 3;
    }
    UsagePeriodBar {
        height: 1;
    }
    StatsFooter {
        height: 3;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("down", "cursor_down", "down", show=False),
        Binding("up", "cursor_up", "up", show=False),
        Binding("h", "cursor_left_or_trajectory", "trajectory", show=False),
        Binding("left", "cursor_left", "left", show=False),
        Binding("right", "cursor_right", "right", show=False),
        Binding("enter", "stage", "stage"),
        Binding("l", "cursor_right_or_focus", "focus", show=False),
        Binding("o", "spawn", "spawn"),
        Binding("x", "kill", "kill"),
        Binding("q", "quit", "quit"),
        Binding(
            REGIE_RETURN_SIGNAL_TEXTUAL,
            "return_to_tree",
            show=False,
            priority=True,
        ),
    ]

    #: ctrl+p opens the palette; ours adds one `Spawn <harness>` entry per registered harness.
    COMMANDS = App.COMMANDS | {SpawnCommand, ViewCommands, ResumeDeadSessionCommand}

    title = "theater régie"

    cursor: reactive[int] = reactive(0)
    tree_lines: reactive[list[tuple[Content, dict, Key, str, str]]] = reactive([])
    #: Whether the bus panel is showing — a once-a-session palette decision; hiding pauses the poll.
    bus_visible: reactive[bool] = reactive(False)
    #: The pane id currently on stage (joined into our window), or None.
    staged_pane: reactive[str | None] = reactive(None)

    def __init__(self, settings: Config | None = None):
        super().__init__()
        self._client: DaemonClient | None = None
        #: The whole config, not just [regie]: palette needs theater.favourite. Injectable.
        self.settings = settings or Config()
        self._cost_window_hours = _COST_WINDOWS.get(self.settings.regie.cost_window, 24.0)
        self._cost_window_period = "day"
        self._cost_window_label = _COST_WINDOW_LABELS["day"]
        #: What the daemon can spawn, or None (before mount/failure); palette reads None as local.
        self.harnesses: list[dict] | None = None
        self.bus_visible = self.settings.regie.bus_visible
        #: Controllers extracted from the composition root.
        self._polling = PollingController(regie=self.settings.regie)
        self._anim_ctrl = RouteAnimationController()
        #: Bumped whenever tree_lines is replaced; shared with the animation controller.
        self._tree_revision = 0
        #: Runs only while something is in flight — an idle régie costs no frames.
        self._anim_timer: Timer | None = None
        self._leaf_reveal = LeafRevealController(enabled=self.settings.regie.startup_reveal)
        self._leaf_reveal_timer: Timer | None = None
        self._leaf_retirement = LeafRetirementController(enabled=self.settings.regie.startup_reveal)
        self._leaf_retirement_timer: Timer | None = None
        self._nav = NavigationState()
        self._usage = UsagePanelState()
        self._staging = StageController(self.settings.regie, panes)
        self._surface = SurfaceController(panes)
        self._trajectory_states = TrajectoryStateStore(
            detail_ratio=self.settings.regie.trajectory_inspector_ratio,
            page_size=self.settings.regie.trajectory_page_size,
        )
        self._trajectory_controller: TrajectoryController | None = None
        self._trajectory_view_widget: TrajectoryView | None = None
        self._session = SessionController(tmux, panes)

    @property
    def bus_cursor(self) -> int:
        return self._polling.bus_cursor

    @bus_cursor.setter
    def bus_cursor(self, value: int) -> None:
        self._polling.bus_cursor = value

    @property
    def anim_cursor(self) -> int:
        return self._polling.anim_cursor

    @anim_cursor.setter
    def anim_cursor(self, value: int) -> None:
        self._polling.anim_cursor = value

    @property
    def _anim_primed(self) -> bool:
        return self._polling._anim_primed

    @_anim_primed.setter
    def _anim_primed(self, value: bool) -> None:
        self._polling._anim_primed = value

    @property
    def _usage_keyboard_metric(self) -> str | None:
        return self._nav.metric

    @_usage_keyboard_metric.setter
    def _usage_keyboard_metric(self, value: str | None) -> None:
        self._nav.metric = value

    @property
    def _usage_keyboard_origin(self) -> str | None:
        return self._nav.origin

    @_usage_keyboard_origin.setter
    def _usage_keyboard_origin(self, value: str | None) -> None:
        self._nav.origin = value

    @property
    def _usage_pointer_metric(self) -> str | None:
        return self._usage.pointer_metric

    @_usage_pointer_metric.setter
    def _usage_pointer_metric(self, value: str | None) -> None:
        self._usage.set_pointer(value)

    @property
    def _usage_active_metric(self) -> str | None:
        return self._usage.active_metric

    @_usage_active_metric.setter
    def _usage_active_metric(self, value: str | None) -> None:
        self._usage.active_metric = value

    @property
    def _usage_breakdown(self) -> dict | None:
        return self._usage.breakdown

    @_usage_breakdown.setter
    def _usage_breakdown(self, value: dict | None) -> None:
        self._usage.breakdown = value

    @property
    def _usage_breakdown_message(self) -> str | None:
        return self._usage.message

    @_usage_breakdown_message.setter
    def _usage_breakdown_message(self, value: str | None) -> None:
        self._usage.message = value

    @property
    def _usage_breakdown_generation(self) -> int:
        return self._usage.generation

    @_usage_breakdown_generation.setter
    def _usage_breakdown_generation(self, value: int) -> None:
        self._usage.generation = value

    def compose(self) -> ComposeResult:
        # No Header: restates the class name; footer already shows usage totals.
        with Vertical(id="sidebar"):
            with TreeStack(id="tree-stack"):
                yield TreePanel(id="tree-panel")
                yield UsageBreakdownPanel(id="usage-breakdown")
            yield UsagePeriodBar(id="usage-period")
            yield StatsFooter(id="stats-footer")
            yield PriceFooter(id="price-footer")
            bus = RichLog(id="bus-panel", max_lines=200, wrap=False, markup=True)
            bus.can_focus = False
            # Applied here: a reactive default fires no watcher; false-vs-false would mount visible.
            bus.set_class(not self.bus_visible, "-hidden")
            yield bus
        with Vertical(id="right-surface"):
            yield WelcomeDashboard(
                self.harnesses,
                sentences=self.settings.regie.dashboard_sentences,
                sentence_hold_seconds=self.settings.regie.dashboard_sentence_hold_seconds,
                sentence_char_interval=self.settings.regie.dashboard_sentence_char_interval,
                tip_hold_seconds=self.settings.regie.dashboard_tip_hold_seconds,
                tip_char_interval=self.settings.regie.dashboard_tip_char_interval,
                id="welcome-dashboard",
            )

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        """Replace Textual's keys submenu with the unstaged dashboard tips."""
        for command in super().get_system_commands(screen):
            if command.title != REGIE_PALETTE_KEYS_COMMAND_TITLE:
                yield command

    async def on_mount(self) -> None:
        self._client = DaemonClient()
        try:
            await self._client.connect()
        except ConnectionError as exc:
            logger.warning("daemon connection failed during régie startup: %s", exc)
            self.notify(f"daemon connection failed: {exc}", severity="error")
            self.exit()
            return
        # Width is imperative: App.CSS is parsed once; must match action_stage.
        self.query_one("#sidebar").styles.width = self.settings.regie.sidebar_width
        # Discover pane/window/session and set up tmux options through the app's own wrappers.
        await self._session.discover_and_setup(
            bind_return_key=self._bind_return_key,
            enable_mouse=self._enable_mouse,
            hide_status=self._hide_status,
        )
        self._apply_theme()
        self._cost_window_hours = self._validate_cost_window()
        self.query_one("#usage-period", UsagePeriodBar).period_label = self._cost_window_label
        await self._load_harnesses()
        self.set_interval(self.settings.regie.tree_interval, self._refresh_tree)
        self.set_interval(self.settings.regie.bus_interval, self._refresh_bus)
        self.set_interval(self.settings.regie.bus_interval, self._refresh_anim)
        self.set_interval(USAGE_INTERVAL, self._refresh_usage)
        await self._refresh_tree()
        await self._refresh_bus()
        await self._refresh_usage()
        # Primes the animation cursor: existing log predates the régie and is not news.
        await self._refresh_anim()

    def on_usage_metric_tile_hovered(self, message: UsageMetricTile.Hovered) -> None:
        """Let the pointer temporarily own the usage panel."""
        self._usage_pointer_metric = message.metric
        self._sync_usage_metric()

    def on_usage_metric_tile_clicked(self, message: UsageMetricTile.Clicked) -> None:
        """Toggle the shared detailed view from any point in a usage tile."""
        self._usage_pointer_metric = message.metric
        self._sync_usage_metric()
        self._toggle_usage_detailed()

    def _render_usage_breakdown(self) -> None:
        metric = self._usage_active_metric
        if metric is None:
            return
        if self._usage.detailed:
            result = self._usage.detailed_breakdown
            message = self._usage.detailed_message
        else:
            result = self._usage_breakdown
            message = self._usage_breakdown_message
        self.query_one("#usage-breakdown", UsageBreakdownPanel).render_state(
            metric,
            result=result,
            message=message,
            detailed=self._usage.detailed,
        )

    def _ensure_compact_usage_fetch(self) -> None:
        generation = self._usage.begin_compact_fetch()
        if generation is not None:
            self.run_worker(self._fetch_usage_breakdown(generation), exclusive=False)

    def _ensure_detailed_usage_fetch(self) -> None:
        generation = self._usage.begin_detailed_fetch()
        if generation is not None:
            self.run_worker(self._fetch_detailed_usage_breakdown(generation), exclusive=False)

    def _activate_usage_metric(self, metric: str) -> None:
        outcome = self._usage.activate(metric)
        if outcome is not ActivateOutcome.NO_CHANGE:
            for tile in self.query(UsageMetricTile):
                tile.set_class(tile.metric == metric, "-hot")
        panel = self.query_one("#usage-breakdown", UsageBreakdownPanel)
        panel.set_class(True, "-visible")
        if outcome is ActivateOutcome.FIRST_OPEN:
            self._constrain_usage_breakdown()
            self._usage.begin_first_open()
            self._render_usage_breakdown()
            if self._usage.detailed:
                self._ensure_detailed_usage_fetch()
            else:
                self._ensure_compact_usage_fetch()
        elif outcome is ActivateOutcome.SWITCH:
            self._render_usage_breakdown()
            if self._usage.detailed:
                self._ensure_detailed_usage_fetch()
            else:
                self._ensure_compact_usage_fetch()

    def _toggle_usage_detailed(self) -> None:
        if self._usage_active_metric is None:
            return
        self._usage.toggle_detailed()
        self._render_usage_breakdown()
        if self._usage.detailed:
            self._ensure_detailed_usage_fetch()
        else:
            self._ensure_compact_usage_fetch()

    def _sync_usage_metric(self) -> None:
        outcome = self._usage.sync(self._usage_keyboard_metric)
        if outcome is SyncOutcome.ACTIVATE:
            metric = self._usage.resolve_metric(self._usage_keyboard_metric)
            assert metric is not None
            self._activate_usage_metric(metric)
            return
        if outcome is SyncOutcome.CLOSE:
            for tile in self.query(UsageMetricTile):
                tile.set_class(False, "-hot")
            self._usage.clear()
            with contextlib.suppress(Exception):
                self.query_one("#usage-breakdown", UsageBreakdownPanel).set_class(False, "-visible")

    def on_usage_metric_tile_left(self, _message: UsageMetricTile.Left) -> None:
        """Defer the close so crossing between adjacent tiles never flickers."""
        self.call_after_refresh(self._hide_usage_breakdown_if_unhovered)

    def on_usage_breakdown_panel_left(self, _message: UsageBreakdownPanel.Left) -> None:
        """Close after leaving the panel unless the pointer returned to a tile."""
        self.call_after_refresh(self._hide_usage_breakdown_if_unhovered)

    def _constrain_usage_breakdown(self) -> None:
        """Clamp the overlay to its tree stack."""
        panel = self.query_one("#usage-breakdown", UsageBreakdownPanel)
        stack = self.query_one("#tree-stack", Vertical)
        panel.constrain_to_height(stack.size.height)

    def _hide_usage_breakdown_if_unhovered(self) -> None:
        node: DOMNode | None = self.mouse_over
        while node is not None:
            if isinstance(node, UsageMetricTile):
                self._usage_pointer_metric = node.metric
                self._sync_usage_metric()
                return
            if isinstance(node, UsageBreakdownPanel):
                return
            node = node.parent
        self._usage_pointer_metric = None
        self._sync_usage_metric()

    async def _fetch_usage_breakdown(self, generation: int) -> None:
        """Fetch one snapshot; stale hover sessions are never allowed to repaint."""
        if self._client is None:
            return
        fetched = await UsageQueries(self._client).fetch_breakdown()
        accepted = (
            self._usage.accept_fetch(
                generation=generation, result=fetched.result, message=fetched.message
            )
            is FetchAccept.ACCEPTED
        )
        if accepted and not self._usage.detailed:
            self._render_usage_breakdown()

    async def _fetch_detailed_usage_breakdown(self, generation: int) -> None:
        """Fetch and retain one detailed snapshot for all five metrics."""
        if self._client is None:
            return
        fetched = await UsageQueries(self._client).fetch_detailed_breakdown()
        accepted = (
            self._usage.accept_detailed_fetch(
                generation=generation, result=fetched.result, message=fetched.message
            )
            is FetchAccept.ACCEPTED
        )
        if accepted and self._usage.detailed:
            self._render_usage_breakdown()

    async def _load_harnesses(self) -> None:
        """Ask the daemon what it can spawn, for the palette to offer.

        Once at mount, not per keystroke: the palette opens on ctrl+p and must
        be instant, and the answer only changes when the daemon restarts — it
        reads its config at start and never reloads. Failure leaves
        `self.harnesses` at None, which the palette reads as "use the local
        registry" rather than showing an empty list.
        """
        if self._client is None:
            return
        try:
            rows = await self._client.call("harnesses")
            assert isinstance(rows, list)
        except Exception as exc:
            logger.debug("harness list unavailable: %s", exc)
            return
        self.harnesses = rows
        dashboard = self._dashboard()
        if dashboard is not None:
            dashboard.update_harnesses(rows)

    def _apply_theme(self) -> None:
        """Switch to the configured theme, or say why not.

        Unknown names are the one config error not raised at load time: the
        legal values live inside Textual, and importing the TUI stack into the
        daemon to validate a string is a poor trade. So the check happens here,
        where the real alternatives can be listed — and it is a notification
        rather than a crash, because a bad theme name is a cosmetic mistake and
        killing the régie over it would hide every agent on the machine.
        """
        name = self.settings.regie.theme
        if not name:
            return
        available = sorted(self.available_themes)
        if name not in available:
            self.notify(
                f"unknown theme {name!r} — available: {', '.join(available)}",
                title="config",
                severity="warning",
                timeout=10,
            )
            return
        self.theme = name

    def _validate_cost_window(self) -> float:
        """Warn about unknown cost_window values at mount, falling back to 'day'."""
        name = self.settings.regie.cost_window
        if name in _COST_WINDOWS:
            self._cost_window_period = name
            self._cost_window_label = _COST_WINDOW_LABELS[name]
            return _COST_WINDOWS[name]
        self._cost_window_period = "day"
        self._cost_window_label = _COST_WINDOW_LABELS["day"]
        self.notify(
            f"unknown cost_window {name!r} — using 'day'. "
            f"available: {', '.join(sorted(_COST_WINDOWS))}",
            title="config",
            severity="warning",
            timeout=10,
        )
        return _COST_WINDOWS["day"]

    async def on_unmount(self) -> None:
        self._stop_leaf_reveal()
        self._stop_leaf_retirement()
        self._leaf_retirement.clear()
        await self._teardown()
        if self._trajectory_controller is not None:
            await self._trajectory_controller.close()
            self._trajectory_controller = None
        if self._client:
            await self._client.aclose()

    # ---- tmux lifecycle (delegates to SessionController) ----------------

    @property
    def my_pane(self) -> str | None:
        return self._session.my_pane

    @my_pane.setter
    def my_pane(self, value: str | None) -> None:
        self._session.my_pane = value

    @property
    def my_window(self) -> str | None:
        return self._session.my_window

    @my_window.setter
    def my_window(self, value: str | None) -> None:
        self._session.my_window = value

    @property
    def my_session(self) -> str | None:
        return self._session.my_session

    @my_session.setter
    def my_session(self, value: str | None) -> None:
        self._session.my_session = value

    @property
    def my_session_name(self) -> str | None:
        return self._session.my_session_name

    @my_session_name.setter
    def my_session_name(self, value: str | None) -> None:
        self._session.my_session_name = value

    @property
    def _mouse_prev(self) -> str | None:
        return self._session._mouse_prev

    @_mouse_prev.setter
    def _mouse_prev(self, value: str | None) -> None:
        self._session._mouse_prev = value

    @property
    def _mouse_set(self) -> bool:
        return self._session._mouse_set

    @_mouse_set.setter
    def _mouse_set(self, value: bool) -> None:
        self._session._mouse_set = value

    @property
    def _status_prev(self) -> str | None:
        return self._session._status_prev

    @_status_prev.setter
    def _status_prev(self, value: str | None) -> None:
        self._session._status_prev = value

    @property
    def _status_set(self) -> bool:
        return self._session._status_set

    @_status_set.setter
    def _status_set(self, value: bool) -> None:
        self._session._status_set = value

    @property
    def _return_key_set(self) -> bool:
        return self._session._return_key_set

    @_return_key_set.setter
    def _return_key_set(self, value: bool) -> None:
        self._session._return_key_set = value

    @property
    def _torn_down(self) -> bool:
        return self._session._torn_down

    @_torn_down.setter
    def _torn_down(self, value: bool) -> None:
        self._session._torn_down = value

    async def _enable_mouse(self) -> None:
        await self._session._enable_mouse()

    async def _restore_mouse(self) -> None:
        await self._session._restore_mouse()

    async def _hide_status(self) -> None:
        await self._session._hide_status()

    async def _restore_status(self) -> None:
        await self._session._restore_status()

    async def _bind_return_key(self) -> None:
        await self._session._bind_return_key()

    async def _unbind_return_key(self) -> None:
        await self._session._unbind_return_key()

    async def _teardown(self) -> None:
        await self._session.teardown(
            staged_pane=self.staged_pane,
            restore_mouse=self._restore_mouse,
            restore_status=self._restore_status,
            unbind_return_key=self._unbind_return_key,
        )

    async def action_quit(self) -> None:
        """Quit, but put the stage back first.

        This has to happen here and not only in `on_unmount`: by unmount time
        Textual is already shutting the loop down, and an awaited tmux call can
        be cancelled halfway through — which would strand the staged agent in a
        window whose other occupant has exited.
        """
        await self._teardown()
        self.exit()

    # ---- polling -------------------------------------------------------

    async def _refresh_tree(self) -> None:
        if not self._client:
            return
        result = await self._polling.poll_tree(
            self.settings.regie.cwd_segments,
            self._client,
            render_tree,
        )
        if result.lines is None:
            return
        self._sync_leaf_retirement(result.lines)
        self.tree_lines = result.lines
        self._tree_revision += 1
        if self.cursor >= len(self.tree_lines):
            self.cursor = max(0, len(self.tree_lines) - 1)
        self._render_tree()

    async def _legacy_usage_summary(self) -> dict:
        """Compatibility with a pre-upgrade daemon that lacks usage_summary."""
        assert self._client is not None
        return await UsageQueries(self._client).legacy_summary(window=self._cost_window_hours)

    async def _refresh_usage(self) -> None:
        if self._client is None:
            return
        result = await UsageQueries(self._client).fetch_summary(
            window=self._cost_window_hours, period=self._cost_window_period
        )
        if not result.available:
            return
        summary = result.raw
        if not isinstance(summary, dict):
            logger.debug("usage refresh returned %s, expected dict", type(summary).__name__)
            return
        echoed_period = summary.get("period")
        period_label = (
            _COST_WINDOW_LABELS.get(echoed_period) if isinstance(echoed_period, str) else None
        )
        if period_label is None:
            period_label = _COST_WINDOW_ROLLING_LABELS[self._cost_window_period]
        with contextlib.suppress(Exception):
            self.query_one("#usage-period", UsagePeriodBar).period_label = period_label
        windowed = summary.get("windowed")
        average = summary.get("average")
        if isinstance(windowed, dict):
            with contextlib.suppress(Exception):
                self.query_one("#stats-footer", StatsFooter).totals = windowed
            with contextlib.suppress(Exception):
                self.query_one("#price-footer", PriceFooter).totals = windowed
        if isinstance(average, dict):
            active_days = average.get("active_days", USAGE_AVERAGE_WINDOW_DAYS)
            try:
                daily = (
                    average.get("cost_microcents", 0) / MICROCENTS_PER_DOLLAR / active_days
                    if active_days > 0
                    else 0.0
                )
            except (TypeError, ValueError):
                logger.debug("usage refresh returned invalid active-day totals: %r", average)
            else:
                with contextlib.suppress(Exception):
                    self.query_one("#price-footer", PriceFooter).daily_avg = daily

    async def _refresh_bus(self) -> None:
        if not self._client:
            return
        result = await self._polling.poll_bus(self.bus_visible, self._client)
        if result.rows is None:
            return
        rows = result.rows
        if result.gap > 0:
            log = self.query_one("#bus-panel", RichLog)
            log.write(Text(f"... {result.gap} events dropped", style="dim italic"))
        rows_sorted = sorted(rows, key=lambda r: r["id"])
        log = self.query_one("#bus-panel", RichLog)
        variables = self.theme_variables
        for row in rows_sorted:
            log.write(format_bus_line(row, variables=variables))
        self._polling.bus_cursor = result.new_cursor

    # ---- tree-route animation -------------------------------------------

    async def _refresh_anim(self) -> None:
        """Read the bus for visible tree-route events, hidden panel or not."""
        if not self._client:
            return
        result = await self._polling.poll_anim(self._client)
        if result.primed:
            return
        if result.needs_tree:
            await self._refresh_tree()
        for event in result.events:
            if event.kind == "send":
                self.start_route_anim(event.from_id, event.to_id)
            elif event.kind == "await_start":
                self.start_await_anim(event.token, event.handle, event.from_id, event.to_id)
            elif event.kind == "await_end":
                self.stop_await_anim(event.token, event.handle, event.from_id, event.to_id)

    @staticmethod
    def _is_prompted_spawn(row: dict) -> bool:
        """Whether *row* is a child created with a prompt from a visible parent."""
        return PollingController._is_prompted_spawn(row)

    @classmethod
    def _needs_tree_refresh(cls, row: dict) -> bool:
        """Whether *row* animates something the current render may not hold yet."""
        return PollingController._needs_tree_refresh(row)

    def start_route_anim(self, from_id: str | None, to_id: str | None) -> None:
        """Begin a trace travelling from *from_id* to *to_id*, if it can."""
        decision = self._anim_ctrl.start_route(self.tree_lines, from_id, to_id)
        if decision.started and self._anim_timer is None:
            self._anim_timer = self.set_interval(TRACE_ANIM_INTERVAL, self._tick_route_anims)

    def start_await_anim(
        self,
        token: object,
        handle: object,
        from_id: str | None,
        to_id: str | None,
    ) -> None:
        """Begin a grayscale pulse for an await edge, if both ends are visible."""
        decision = self._anim_ctrl.start_await(self.tree_lines, token, handle, from_id, to_id)
        if decision.started and self._anim_timer is None:
            self._anim_timer = self.set_interval(TRACE_ANIM_INTERVAL, self._tick_route_anims)

    def stop_await_anim(
        self,
        token: object,
        handle: object,
        from_id: str | None,
        to_id: str | None,
    ) -> None:
        """End one active await pulse and clear it if it was the last overlay."""
        decision = self._anim_ctrl.stop_await(token, handle, from_id, to_id)
        if decision.clear_overlays:
            panel = self._panel()
            if panel is not None:
                panel.set_overlays({})
        if decision.stop_timer:
            self._stop_anim_timer()

    def _reap_await_anims(self) -> None:
        """Drop pulses whose end row never arrived. See :data:`AWAIT_ANIM_TTL`."""
        self._anim_ctrl._reap_await_anims()

    @property
    def _route_anims(self) -> list[RouteAnim]:
        return self._anim_ctrl.route_anims

    @property
    def _await_anims(self) -> dict[tuple[str, str, str, str], AwaitRouteAnim]:
        return self._anim_ctrl.await_anims

    @property
    def _await_cells(self) -> dict[tuple[str, str], list[AwaitCell] | None]:
        return self._anim_ctrl._await_cells

    @_await_cells.setter
    def _await_cells(self, value: dict[tuple[str, str], list[AwaitCell] | None]) -> None:
        self._anim_ctrl._await_cells = value

    @property
    def _await_cells_revision(self) -> int:
        return self._anim_ctrl._await_cells_revision

    @_await_cells_revision.setter
    def _await_cells_revision(self, value: int) -> None:
        self._anim_ctrl._await_cells_revision = value

    def _await_route_cells(self, from_id: str, to_id: str) -> list[AwaitCell] | None:
        """The await route's visible cells, computed once per tree revision."""
        return self._anim_ctrl._await_route_cells(
            self.tree_lines, self._tree_revision, from_id, to_id, await_highlight_cells
        )

    def _tick_route_anims(self) -> None:
        """Draw every trace where it is now, then advance it one step."""
        result = self._anim_ctrl.tick(self.tree_lines, self._tree_revision, await_highlight_cells)
        panel = self._panel()
        if panel is not None:
            panel.set_overlays(result.overlays)
        if result.stop_timer:
            self._stop_anim_timer()

    def _stop_anim_timer(self) -> None:
        if self._anim_timer is not None:
            self._anim_timer.stop()
            self._anim_timer = None

    # ---- rendering -----------------------------------------------------

    def _panel(self) -> TreePanel | None:
        """The tree panel, or None when there is no mounted widget to draw on.

        Reactive watchers fire on assignment, which can happen before mount and
        during teardown as well as in the middle of a normal frame. A missing
        widget in those windows is expected, not an error.
        """
        try:
            return self.query_one("#tree-panel", TreePanel)
        except Exception:
            return None

    def _dashboard(self) -> WelcomeDashboard | None:
        """Return the mounted welcome dashboard when it exists."""
        try:
            return self.query_one("#welcome-dashboard", WelcomeDashboard)
        except Exception:
            return None

    def _trajectory_view(self) -> TrajectoryView | None:
        view = self._trajectory_view_widget
        return view if view is not None and view.is_mounted else None

    @property
    def right_surface(self) -> RightSurface:
        return self._surface.surface

    @property
    def trajectory_participant(self) -> str | None:
        return self._surface.trajectory_participant

    def _trajectory_highlight(self) -> str | None:
        if self._surface.trajectory_visible(self.staged_pane):
            return self._surface.trajectory_participant
        return None

    def _sync_right_surface(self) -> None:
        trajectory_visible = self._surface.trajectory_visible(self.staged_pane)
        dashboard = self._dashboard()
        if dashboard is not None:
            dashboard.set_staged(self.staged_pane is not None or trajectory_visible)
        view = self._trajectory_view()
        if view is not None:
            view.set_class(not trajectory_visible, "-hidden-surface")
        with contextlib.suppress(Exception):
            self.query_one("#right-surface", Vertical).set_class(
                self.staged_pane is not None,
                "-pane-staged",
            )

    def _ensure_trajectory_controller(self) -> TrajectoryController:
        if self._trajectory_controller is None:
            self._trajectory_controller = TrajectoryController(
                DaemonClient(),
                DaemonClient(),
                state_store=self._trajectory_states,
            )
        return self._trajectory_controller

    async def _mount_trajectory(self, participant_id: str) -> TrajectoryView:
        current = self._trajectory_view()
        if current is not None and current.participant_id == participant_id:
            return current
        if current is not None:
            await current.remove()
        view = TrajectoryView(
            participant_id,
            controller=self._ensure_trajectory_controller(),
            copy_request=self._copy_trajectory,
            focus_on_mount=False,
            id="trajectory-view",
        )
        self._trajectory_view_widget = view
        await self.query_one("#right-surface", Vertical).mount(view)
        self._sync_right_surface()
        return view

    def _selected_trajectory_target(self) -> tuple[str | None, bool]:
        if not 0 <= self.cursor < len(self.tree_lines):
            return None, False
        _, node, key, _, _ = self.tree_lines[self.cursor]
        participant_id = node.get("id") if node else None
        return (participant_id if isinstance(participant_id, str) else None, key[0] == "p")

    async def _show_trajectory(
        self,
        participant_id: str | None,
        *,
        managed: bool,
        focus_after_stage: bool = False,
    ) -> TrajectoryStageResult:
        result = await self._surface.stage_trajectory(
            participant_id=participant_id,
            managed=managed,
            staged_pane=self.staged_pane,
            footer_active=self._nav.in_footer,
        )
        if result.outcome is TrajectoryStageOutcome.NO_NODE:
            self.notify("nothing to inspect", severity="warning")
            return result
        if result.outcome is TrajectoryStageOutcome.UNMANAGED:
            self.notify("adopt this pane before opening its trajectory", severity="warning")
            return result
        if result.outcome is TrajectoryStageOutcome.PARK_FAILED:
            self.notify(f"could not park staged pane: {result.error}", severity="error")
            return result
        if result.outcome is TrajectoryStageOutcome.FOOTER_ACTIVE:
            return result
        if result.staged_pane != self.staged_pane:
            self.staged_pane = result.staged_pane
        assert result.participant_id is not None
        view = await self._mount_trajectory(result.participant_id)
        self._sync_right_surface()
        self._render_tree()
        if result.outcome is TrajectoryStageOutcome.FOCUS or focus_after_stage:
            view.focus_region(view.state.focus_region)
        else:
            self.set_focus(None)
        return result

    async def _copy_trajectory(self, text: str) -> None:
        try:
            await tmux.set_buffer(text)
        except Exception as exc:
            self.notify(f"copy failed: {exc}", severity="error")

    async def on_trajectory_participant_selected(
        self,
        message: TrajectoryParticipantSelected,
    ) -> None:
        for index, (_, node, key, _, _) in enumerate(self.tree_lines):
            if key[0] == "p" and node.get("id") == message.participant_id:
                self._leave_usage_metrics()
                self.cursor = index
                self._render_tree()
                await self._show_trajectory(
                    message.participant_id,
                    managed=True,
                    focus_after_stage=True,
                )
                return
        self.notify("linked participant is no longer in the tree", severity="warning")

    def on_return_to_tree(self, _message: ReturnToTree) -> None:
        self._focus_tree()

    def action_return_to_tree(self) -> None:
        self._focus_tree()

    def _focus_tree(self) -> None:
        if self.is_running:
            self.set_focus(None)
        self._render_tree()

    def _render_tree(self) -> None:
        panel = self._panel()
        if panel is None:
            return
        panel._lines_data = self.tree_lines
        panel.lines = self.tree_lines
        self._sync_leaf_reveal(panel)
        cursor = self._visible_tree_cursor()
        panel.apply_cursor(cursor, self.staged_pane, self._trajectory_highlight())
        panel.scroll_to_cursor(cursor)

    def _sync_leaf_reveal(self, panel: TreePanel) -> None:
        keys = panel.leaf_keys()
        if not self._leaf_reveal.needs_sync(keys):
            return
        agent_spawns = {
            key for _, node, key, _, _ in self.tree_lines if key[0] == "p" and node.get("parent_id")
        }
        frame = self._leaf_reveal.observe(
            panel.reveal_widths(self._leaf_reveal.sync_keys(keys)),
            animate_new=agent_spawns,
        )
        panel.set_reveals(frame.widths)
        if frame.active and self._leaf_reveal_timer is None:
            self._leaf_reveal_timer = self.set_interval(
                STARTUP_REVEAL_INTERVAL, self._tick_leaf_reveal
            )

    def _sync_leaf_retirement(self, lines: list[tuple[Content, dict, Key, str, str]]) -> None:
        """Move disappeared agent-created leaves into the reverse-reveal layer."""
        participants = {
            key: bool(node.get("parent_id")) for _, node, key, _, _ in lines if key[0] == "p"
        }
        change = self._leaf_retirement.observe(participants)
        self._leaf_reveal.cancel(change.retire)
        if not self._leaf_reveal.active:
            self._stop_leaf_reveal()
        panel = self._panel()
        if panel is None:
            self._leaf_retirement.begin({}, candidates=change.retire)
            return
        panel.restore_retiring(change.restore)
        if change.restore and not self._leaf_retirement.active:
            self._stop_leaf_retirement()
        if change.retire:
            frame = self._leaf_retirement.begin(
                panel.retire(change.retire), candidates=change.retire
            )
            self._apply_leaf_retirement(panel, frame)

    def _apply_leaf_retirement(self, panel: TreePanel, frame: LeafRetirementFrame) -> None:
        """Draw one retirement frame and release leaves that reached zero width."""
        panel.set_retiring_reveals(frame.widths)
        panel.finish_retiring(frame.completed)
        if frame.active and self._leaf_retirement_timer is None:
            self._leaf_retirement_timer = self.set_interval(
                STARTUP_REVEAL_INTERVAL, self._tick_leaf_retirement
            )
        if not frame.active:
            self._stop_leaf_retirement()

    def _tick_leaf_retirement(self) -> None:
        panel = self._panel()
        if panel is None:
            self._stop_leaf_retirement()
            return
        self._apply_leaf_retirement(panel, self._leaf_retirement.tick())

    def _stop_leaf_retirement(self) -> None:
        if self._leaf_retirement_timer is not None:
            self._leaf_retirement_timer.stop()
            self._leaf_retirement_timer = None

    def _tick_leaf_reveal(self) -> None:
        panel = self._panel()
        if panel is None:
            return
        keys = panel.leaf_keys()
        frame = self._leaf_reveal.tick(panel.reveal_widths(self._leaf_reveal.sync_keys(keys)))
        panel.set_reveals(frame.widths)
        if not frame.active:
            self._stop_leaf_reveal()

    def _stop_leaf_reveal(self) -> None:
        if self._leaf_reveal_timer is not None:
            self._leaf_reveal_timer.stop()
            self._leaf_reveal_timer = None

    def _visible_tree_cursor(self) -> int:
        return HIDDEN_TREE_CURSOR if self._usage_keyboard_metric is not None else self.cursor

    def _index_for_key(self, key: Key) -> int | None:
        """The line index of *key* in the current tree, or None."""
        for i, (_, _, k, _, _) in enumerate(self.tree_lines):
            if k == key:
                return i
        return None

    def watch_cursor(self, cursor: int) -> None:
        """Redraw the tree with the cursor highlighted."""
        panel = self._panel()
        if panel is None:
            return
        cursor = self._visible_tree_cursor()
        panel.apply_cursor(cursor, self.staged_pane, self._trajectory_highlight())
        panel.scroll_to_cursor(cursor)

    def watch_staged_pane(self, pane: str | None) -> None:
        """Sync physical staging with the right surface and tree marker."""
        self._sync_right_surface()
        panel = self._panel()
        if panel is None:
            return
        panel.apply_cursor(
            self._visible_tree_cursor(),
            self.staged_pane,
            self._trajectory_highlight(),
        )

    def watch_bus_visible(self, visible: bool) -> None:
        """Show or hide the bus panel, giving the tree the space either way."""
        try:
            panel = self.query_one("#bus-panel", RichLog)
        except Exception:
            # Missing widget is expected before mount / during teardown (cf. _panel).
            return
        panel.set_class(not visible, "-hidden")

    # ---- actions -------------------------------------------------------

    def action_toggle_bus(self) -> None:
        """Show or hide the bus panel. Offered by the command palette."""
        self.bus_visible = not self.bus_visible

    def action_cursor_down(self) -> None:
        if self._nav.in_footer:
            old = self._usage_keyboard_metric
            target = self._nav.down()
            if target != old:
                assert target is not None
                self._select_usage_metric(target)
            return
        if self.cursor < len(self.tree_lines) - 1:
            self.cursor += 1
            self._render_tree()
            return
        target = self._nav.down()
        assert target is not None
        self._select_usage_metric(target)

    def action_cursor_up(self) -> None:
        result = self._nav.up()
        if result is UpDecision.LEAVE:
            self._leave_usage_metrics()
            return
        if result is not None:
            self._select_usage_metric(result)
            return
        if self.cursor > 0:
            self.cursor -= 1
            self._render_tree()

    def action_cursor_left(self) -> None:
        target = self._nav.left()
        if target is not None:
            self._select_usage_metric(target)

    async def action_cursor_left_or_trajectory(self) -> None:
        if self._nav.in_footer:
            self.action_cursor_left()
            return
        participant_id, managed = self._selected_trajectory_target()
        await self._show_trajectory(participant_id, managed=managed)

    def action_cursor_right(self) -> None:
        target = self._nav.right()
        if target is not None:
            self._select_usage_metric(target)

    async def action_cursor_right_or_focus(self) -> None:
        if self._nav.in_footer:
            self.action_cursor_right()
        else:
            await self.action_focus_stage()

    def _select_usage_metric(self, metric: str) -> None:
        self._nav.select(metric)
        self._render_tree()
        self._sync_usage_metric()

    def _leave_usage_metrics(self) -> None:
        self._nav.leave()
        self._render_tree()
        self._sync_usage_metric()

    def _notify_stage_result(self, result: StageResult | None) -> None:
        """Issue the same notifications inline staging used to produce."""
        if result is None:
            return
        if result.outcome is StageOutcome.NO_NODE:
            self.notify("nothing to stage", severity="warning")
        elif result.outcome is StageOutcome.NO_PANE:
            self.notify(f"{(result.node_id or '?')[:8]} has no pane", severity="warning")
        elif result.outcome is StageOutcome.NO_WINDOW:
            self.notify("régie window not discovered — cannot stage", severity="error")
        elif result.outcome is StageOutcome.UNSTAGE_FAILED:
            self.notify(f"unstage failed: {result.error}", severity="error")
        elif result.outcome is StageOutcome.JOIN_FAILED:
            self.notify(f"stage failed: {result.error}", severity="error")

    async def action_stage(self) -> None:
        """Stage the selected agent: join its pane into the régie's window.

        Delegates staging mechanics to ``StageController`` and performs the
        app-side reactions (notifications, reactive assignment) based on the
        returned outcome.
        """
        if self._usage_keyboard_metric is not None:
            self._toggle_usage_detailed()
            return
        result = await self._staging.stage(
            tree_lines=self.tree_lines,
            cursor=self.cursor,
            staged_pane=self.staged_pane,
            my_window=self.my_window,
            my_pane=self.my_pane,
            footer_active=self._usage_keyboard_metric is not None,
            selected_participant_fn=selected_participant,
        )
        if result.staged_pane != self.staged_pane:
            self.staged_pane = result.staged_pane
        self._notify_stage_result(result)

    async def action_kill(self) -> None:
        """Kill the selected participant."""
        if self._usage_keyboard_metric is not None:
            return
        node = selected_participant(self.tree_lines, self.cursor)
        if node is None:
            self.notify("nothing to kill", severity="warning")
            return
        pid = node.get("id")
        pane = node.get("tmux_pane")
        if not pid:
            self.notify("cannot kill an unmanaged pane", severity="warning")
            return
        if not self._client:
            return
        try:
            await self._client.call("participant.kill", id=pid)
        except Exception as exc:
            self.notify(f"kill failed: {exc}", severity="error")
        else:
            if pane == self.staged_pane:
                self.staged_pane = None
            self._leaf_retirement.remove_without_animation(("p", pid))
        await self._refresh_tree()

    def action_spawn(self) -> None:
        from textual.command import CommandPalette

        self.push_screen(
            CommandPalette(
                providers=[SpawnHarnessCommands],
                placeholder="Spawn a fresh session\u2026",
            ),
        )

    def spawn_harness(self, harness: str) -> None:
        """Start a bare CLI of this harness, from the command palette.

        Sync on purpose: a palette hit runs its command as a plain callback,
        and handing it a worker rather than a coroutine keeps the palette from
        having to care whether the daemon is slow to answer.
        """
        self.run_worker(self._spawn_harness(harness), exclusive=False)

    async def _spawn_harness(self, harness: str) -> None:
        self._focus_tree()
        if not self._client:
            return
        try:
            await self._client.call(
                "spawn",
                harness=harness,
                # No prompt/parent: palette starts a CLI; manual adds no approval flags.
                prompt="",
                approval="manual",
                cwd=str(Path.cwd()),
                # By name, only ours: new window goes in the user's session, not the fallback.
                tmux_session=self.my_session_name,
            )
        except Exception as exc:
            self.notify(f"spawn failed: {exc}", severity="error")
            self._focus_tree()
            return
        # The new agent appears in the tree on the next refresh.
        await self._refresh_tree()
        self._focus_tree()

    async def load_dead_sessions(self) -> list[dict]:
        if self._client is None:
            return []
        try:
            rows = await self._client.call("participants.recent_dead", limit=20)
            assert isinstance(rows, list)
        except Exception:
            return []
        return rows

    def action_resume_dead_session(self) -> None:
        from textual.command import CommandPalette

        self.push_screen(
            CommandPalette(
                providers=[ResumeDeadSessionCommands],
                placeholder="Search for dead sessions\u2026",
            ),
        )

    def resume_dead_session(self, row: dict) -> None:
        state = row.get("resume_state", "")
        if state != "resumable":
            reasons = {
                "live": "session is still running",
                "no_session_id": "no session id was recorded",
                "harness_cannot_resume": "this harness does not support resume",
                "untrusted": "transcript identity could not be verified",
                "owned_by_live": "another live session holds this session id",
            }
            reason = reasons.get(state, state or "unknown reason")
            self.notify(
                f"Cannot resume: {reason}",
                title="Session not resumable",
                severity="warning",
            )
            return
        if not row.get("cwd"):
            self.notify("Cannot resume: original cwd is missing", severity="warning")
            return
        self.run_worker(self._resume_dead_session(row), exclusive=False)

    async def _resume_dead_session(self, row: dict) -> None:
        self._focus_tree()
        if self._client is None:
            return
        try:
            await self._client.call(
                "spawn",
                harness=row["harness"],
                prompt="",
                cwd=row["cwd"],
                approval="manual",
                resume=row["session_id"],
                worktree=False,
                tmux_session=self.my_session_name,
            )
        except Exception as exc:
            self.notify(f"resume failed: {exc}", severity="error")
            self._focus_tree()
            return
        await self._refresh_tree()
        self._focus_tree()

    async def action_focus_stage(self) -> None:
        """Stage on first `l`; focus an already staged pane on second `l`."""
        result = await self._staging.focus(
            tree_lines=self.tree_lines,
            cursor=self.cursor,
            staged_pane=self.staged_pane,
            my_window=self.my_window,
            my_pane=self.my_pane,
            footer_active=self._usage_keyboard_metric is not None,
            selected_participant_fn=selected_participant,
        )
        if result.staged_pane != self.staged_pane:
            self.staged_pane = result.staged_pane
        self._notify_stage_result(result.stage_result)
        if result.should_select and result.pane:
            try:
                await panes.select_pane(result.pane)
            except Exception as exc:
                self.notify(f"focus failed: {exc}", severity="error")

    # ---- tree rendering with cursor ------------------------------------


def run_regie(settings: Config | None = None) -> None:
    """Entry point for `theater regie`."""
    app = RegieApp(settings)
    app.run()
