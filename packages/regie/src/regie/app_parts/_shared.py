"""The Régie logger and the typing-only host that every RegieApp mixin extends."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

logger = logging.getLogger("regie")

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from textual.app import App
    from textual.timer import Timer

    from regie.animations.routes import RouteAnimationController
    from regie.bus import DiagnosticBusController
    from regie.client_pool import FrontendClientPool
    from regie.contracts import PresentationOperations, RegieSettings, UnmanagedPane
    from regie.controllers.action_presentation import ActionPresentation
    from regie.controllers.actions import ActionRecord, OperationController
    from regie.controllers.navigation import NavigationState
    from regie.controllers.polling import RefreshGate
    from regie.controllers.presentation_queue import PresentationQueue
    from regie.controllers.staging import StageController, StageResult
    from regie.controllers.state_follow import StateFollowLoop
    from regie.controllers.surface import SurfaceController
    from regie.controllers.transcripts import TranscriptBindingController
    from regie.controllers.usage import UsagePanelState
    from regie.resume import ResumeCandidate
    from regie.state import StateController
    from regie.trajectory.rich import TrajectoryController, TrajectoryNavigationHistory
    from regie.trajectory.rich.view import TrajectoryView
    from regie.tree_layout import TreeLayout
    from regie.usage import UsageController
    from theater.frontend import StateProjection
    from theater.frontend.dto.catalogs import HarnessCatalogEntry

    class RegieHost(App[None]):
        """State set by ``RegieApp.__init__`` and methods one mixin calls on another."""

        settings: RegieSettings
        presentation: PresentationOperations
        _startup_started_at: float
        _initial_projection_pending: bool
        _clients: FrontendClientPool
        _state: StateController
        _actions: OperationController
        _staging: StageController
        _presentation_queue: PresentationQueue
        _bus: DiagnosticBusController
        _animation_bus: DiagnosticBusController
        _animation_primed: bool
        _animation: RouteAnimationController
        _animation_timer: Timer | None
        _trajectory: TrajectoryController
        _trajectory_view_widget: TrajectoryView | None
        _trajectory_navigation: TrajectoryNavigationHistory
        _usage: UsageController
        _usage_panel: UsagePanelState
        _transcript_bindings: TranscriptBindingController
        _navigation: NavigationState
        _surface: SurfaceController
        _sync_gate: RefreshGate
        _state_follow: StateFollowLoop
        _catalog_lock: asyncio.Lock
        _controls_inspection_lock: asyncio.Lock
        _resume_discovery_lock: asyncio.Lock
        _transcript_candidates_lock: asyncio.Lock
        _harnesses: tuple[HarnessCatalogEntry, ...]
        _transcript_recovery_target: str | None
        _unmanaged: tuple[UnmanagedPane, ...] | None
        _unmanaged_polled_at: float | None
        _bus_visible: bool
        _usage_visible: bool
        _last_state_error: tuple[str, str] | None
        _action_presentation: ActionPresentation
        _startup_task: asyncio.Task[None] | None
        _catalog_ready: asyncio.Event
        _projection_ready: asyncio.Event
        _closed: bool
        _tree_layout: TreeLayout
        _tree_layout_path: Path | None

        @property
        def _view_active(self) -> bool: ...
        @property
        def _installed_harnesses(self) -> tuple[HarnessCatalogEntry, ...]: ...
        @staticmethod
        def _participant_id_for_target(
            target: object, projection: StateProjection
        ) -> str | None: ...
        def _finish_initial_projection(self) -> None: ...
        def _initialize_tree_layout(self) -> None: ...
        def delete_separator(self, separator_id: str) -> None: ...
        def _separator_selected(self) -> bool: ...
        def toggle_separator(self, separator_id: str) -> None: ...
        def rename_separator(self, separator_id: str, name: str) -> None: ...
        async def _load_catalog(self) -> bool: ...
        async def _refresh_usage(self) -> None: ...
        def _select_usage_metric(self, metric: str, *, origin: str | None = None) -> None: ...
        def _leave_usage_metrics(self) -> None: ...
        def _toggle_usage_detailed(self) -> None: ...
        async def _tick_synchronize(self) -> None: ...
        async def _refresh_local_projection(self) -> None: ...
        async def _refresh_unmanaged(
            self, projection: StateProjection, *, force: bool = False
        ) -> None: ...
        def _show_projection(self, projection: StateProjection) -> None: ...
        def _show_state_error(self, exc: Exception) -> None: ...
        def _sync_surface(self) -> None: ...
        async def _refresh_bus(self) -> None: ...
        async def _refresh_animations(self) -> None: ...
        def _selected_id(self) -> str | None: ...
        def _selected_unmanaged_pane(self) -> str | None: ...
        def select_participant(self, participant_id: str) -> None: ...
        def action_cursor_left(self) -> None: ...
        def action_cursor_right(self) -> None: ...
        def _restore_tree_focus(self) -> None: ...
        async def action_focus_stage(self) -> None: ...
        async def _stage_selected(
            self, mode: str, participant_id: str | None, unmanaged: str | None
        ) -> None: ...
        def _submit_presentation(
            self, action: str, work: Callable[[], Awaitable[object]]
        ) -> None: ...
        def _show_stage_result(self, result: StageResult | None) -> None: ...
        def _trajectory_view(self) -> TrajectoryView | None: ...
        def _trajectory_has_focus(self) -> bool: ...
        def _trajectory_request(self, mode: str) -> Callable[[], Awaitable[None]] | None: ...
        async def submit_spawn(
            self, harness: str, prompt: str, approval: str, *, cwd: str
        ) -> ActionRecord: ...
        async def submit_resume(
            self, candidate: ResumeCandidate, prompt: str, approval: str
        ) -> ActionRecord: ...
        def _start_action(self, awaitable: object) -> None: ...
        def _render_pending_actions(self) -> None: ...
        def _set_status(self, message: str) -> None: ...

    _AppBase = RegieHost
else:
    _AppBase = object

__all__ = ["_AppBase", "logger"]
