"""State-follow synchronization and projection rendering into the tree."""

from __future__ import annotations

from time import monotonic

from textual.containers import Vertical

from regie.app_parts._shared import _AppBase, logger
from regie.contracts import LocalPresentationTarget
from regie.controllers.surface import SurfaceMode
from regie.dashboard import WelcomeDashboard
from regie.latency import startup_milestone
from regie.presentation import stageability
from regie.ui_constants import REGIE_UNMANAGED_POLL_INTERVAL_SECONDS
from regie.widgets import ParticipantTree
from theater.frontend import (
    FrontendClientError,
    FrontendResponseError,
    FrontendTransportError,
    StateProjection,
    StateSynchronizationError,
)


class ProjectionSync(_AppBase):
    async def _tick_synchronize(self) -> None:
        if not self._view_active:
            return
        await self._sync_gate.run(self._synchronize_projection)

    async def _synchronize_projection(self, *, following: bool = False) -> bool:
        if not self._view_active:
            return False
        previous = self._state.projection
        was_stale = previous is not None and previous.stale
        try:
            projection = (
                await self._state.follow() if following else await self._state.synchronize()
            )
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
            return False
        if not self._view_active or projection is None:
            return False
        changed = previous is None or previous.cursor != projection.cursor or was_stale
        self._last_state_error = None
        if following and not changed and not projection.catalog_dirty:
            return False
        self._show_projection(projection)
        self._render_pending_actions()
        decorations = (self._harnesses, self._unmanaged, self._staging.staged_target)
        if was_stale and not projection.stale:
            await self._actions.refresh_pending()
        await self._refresh_catalog_if_dirty(projection)
        await self._refresh_unmanaged(projection)
        current = self._state.projection or projection
        if current is not projection or decorations != (
            self._harnesses,
            self._unmanaged,
            self._staging.staged_target,
        ):
            self._show_projection(current)
        self._render_pending_actions()
        return changed

    async def _refresh_local_projection(self) -> None:
        projection = self._state.projection
        if not self._view_active or projection is None:
            return
        await self._refresh_unmanaged(projection)
        if (current := self._state.projection) is not None:
            self._show_projection(current)
        self._render_pending_actions()

    async def _refresh_unmanaged(self, projection: StateProjection, *, force: bool = False) -> None:
        """Refresh local-only panes independently from the public state stream."""
        if not self._view_active:
            return
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
        if not self._view_active:
            return
        projection = self._state.projection or projection
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

    def _show_projection(self, projection: StateProjection) -> None:
        if not self._view_active:
            return
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
        managed = {
            route.identity.terminal_id
            for participant in projection.participants.values()
            if (route := participant.terminal_route) is not None
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
            layout=self._tree_layout,
            unmanaged=[
                {**pane.to_tree_row(), "icon": harness_icons.get(pane.harness or "")}
                for pane in self._unmanaged or ()
                if pane.pane_id not in managed
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
        if trajectory is not None and self._surface.mode is not SurfaceMode.TRAJECTORY:
            # A trajectory that is no longer shown is closed, not kept live in the background.
            self._trajectory_view_widget = None
            self._trajectory_navigation.clear()
            trajectory.remove()
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
