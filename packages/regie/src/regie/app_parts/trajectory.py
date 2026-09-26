"""Trajectory view: open, close, focus, and link navigation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import partial
from typing import TYPE_CHECKING

from textual.containers import Vertical

from regie.app_parts._shared import _AppBase
from regie.controllers.staging import StageOutcome
from regie.controllers.surface import SurfaceMode
from regie.trajectory.rich import (
    ReturnToTree,
    TrajectoryBackRequested,
    TrajectoryCopyRequested,
    TrajectoryParticipantSelected,
    TrajectoryRetryRequested,
)
from regie.widgets import ParticipantTree
from theater.frontend.trajectory import TrajectoryLocationResolution

if TYPE_CHECKING:
    from regie.trajectory.rich.view import TrajectoryView


class TrajectoryActions(_AppBase):
    def _trajectory_view(self) -> TrajectoryView | None:
        view = self._trajectory_view_widget
        return view if view is not None and view.is_mounted else None

    def _trajectory_has_focus(self) -> bool:
        view = self._trajectory_view()
        return view is not None and view.has_focus_within

    async def _mount_trajectory(self, participant_id: str) -> TrajectoryView:
        from regie.trajectory.rich.view import TrajectoryView

        current = self._trajectory_view()
        if current is not None and current.participant_id == participant_id:
            return current
        if current is not None:
            await current.remove()
        view = TrajectoryView(
            participant_id,
            controller=self._trajectory,
            copy_request=self._copy_trajectory,
            participant_identity=self._trajectory_participant_identity,
            focus_on_mount=False,
            id="trajectory-view",
        )
        self._trajectory_view_widget = view
        surface = self.query_one("#right-surface", Vertical)
        await surface.mount(view)
        return view

    def _trajectory_participant_identity(
        self, participant_id: str
    ) -> tuple[str | None, str | None]:
        projection = self._state.projection
        participant = None if projection is None else projection.participants.get(participant_id)
        if participant is None:
            return None, None
        return participant.name, participant.harness

    async def open_trajectory(self, participant_id: str) -> TrajectoryView | None:
        return await self._presentation_queue.run(
            "trajectory", partial(self._open_trajectory, participant_id)
        )

    async def _open_trajectory(self, participant_id: str) -> TrajectoryView | None:
        if self._staging.staged_target is not None:
            result = await self._staging.unstage()
            self._show_stage_result(result)
            if result.outcome is not StageOutcome.UNSTAGED:
                return None
        if not self._view_active:
            return None
        view = await self._mount_trajectory(participant_id)
        self._surface.show_trajectory(participant_id)
        view.enter_live_tail()
        self._sync_surface()
        return view

    def action_request_trajectory(self, mode: str) -> None:
        if mode == "left" and self._usage_panel.in_footer:
            self.action_cursor_left()
            return
        if mode == "left" and self._trajectory_has_focus():
            return
        if (work := self._trajectory_request(mode)) is not None:
            self._submit_presentation("trajectory", work)

    def _trajectory_request(self, mode: str) -> Callable[[], Awaitable[None]] | None:
        participant_id = self._selected_id()
        if participant_id is None and self._separator_selected():
            return None
        if participant_id is None:
            self.notify(
                "adopt this pane before opening its trajectory"
                if self._selected_unmanaged_pane() is not None
                else "nothing to inspect",
                severity="warning",
            )
            return None
        return partial(self._show_selected_trajectory, participant_id, mode)

    async def _show_selected_trajectory(self, participant_id: str, mode: str) -> None:
        if not self._view_active:
            return
        if (
            mode in {"left", "toggle"}
            and self._surface.mode is SurfaceMode.TRAJECTORY
            and self._surface.trajectory_participant_id == participant_id
        ):
            if mode == "toggle":
                self._surface.show_dashboard()
                self._sync_surface()
            else:
                view = self._trajectory_view()
                if view is not None:
                    view.focus_region(view.state.focus_region)
            return
        self._trajectory_navigation.clear()
        view = await self._open_trajectory(participant_id)
        if view is not None:
            if mode == "open":
                view.focus_region(view.state.focus_region)
            else:
                self.set_focus(None)

    async def action_toggle_trajectory(self) -> None:
        if (work := self._trajectory_request("toggle")) is not None:
            await self._presentation_queue.run("trajectory", work)

    async def action_stage_and_focus_trajectory(self) -> None:
        if (work := self._trajectory_request("open")) is not None:
            await self._presentation_queue.run("trajectory", work)

    async def action_return_to_tree(self) -> None:
        """The tmux return key (prefix h); from a focused trajectory it leaves it, like Esc."""
        if self._trajectory_has_focus():
            self._leave_trajectory()
            return
        self.set_focus(None)
        self.query_one(ParticipantTree).set_cursor_visible(True)

    def on_return_to_tree(self, _message: ReturnToTree) -> None:
        self._leave_trajectory()

    def _leave_trajectory(self) -> None:
        """Leaving a trajectory closes it and returns to the dashboard."""
        self._surface.show_dashboard()
        self._sync_surface()
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
