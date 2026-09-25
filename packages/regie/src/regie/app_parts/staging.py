"""Staging terminals into the presentation surface."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import partial

from regie.app_parts._shared import _AppBase
from regie.contracts import LocalPresentationTarget
from regie.controllers.staging import StageOutcome, StageResult
from regie.presentation import stageability, target_for_participant
from theater.frontend import (
    FrontendClientError,
    FrontendResponseError,
    FrontendTransportError,
    StateSynchronizationError,
)


class StagingActions(_AppBase):
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
        if target_for_participant(participant, projection.providers) is None:
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
                return StageResult(
                    StageOutcome.UNAVAILABLE, None, "public terminal route refresh failed"
                )
            if self._view_active:
                self._show_projection(projection)
            participant = projection.participants.get(participant_id)
            if participant is None:
                return None
        return await self._staging.stage(participant, projection.providers)

    async def action_stage(self) -> None:
        if self._usage_panel.in_footer:
            self._toggle_usage_detailed()
            return
        await self._presentation_queue.run("toggle", self._stage_request("toggle"))

    async def action_focus_stage(self) -> None:
        await self._presentation_queue.run("focus", self._stage_request("focus"))

    async def action_stage_and_focus_tmux(self) -> None:
        await self._presentation_queue.run("open", self._stage_request("open"))

    def _stage_request(self, mode: str) -> Callable[[], Awaitable[None]]:
        return partial(
            self._stage_selected, mode, self._selected_id(), self._selected_unmanaged_pane()
        )

    def action_request_presentation(self, mode: str) -> None:
        if mode in {"toggle", "focus"} and self._usage_panel.in_footer:
            if mode == "toggle":
                self._toggle_usage_detailed()
            elif mode == "focus":
                self.action_cursor_right()
            return
        if mode == "focus" and self._trajectory_has_focus():
            return
        self._submit_presentation(mode, self._stage_request(mode))

    def _submit_presentation(self, action: str, work: Callable[[], Awaitable[object]]) -> None:
        self._presentation_queue.submit(action, work).add_done_callback(self._presentation_finished)

    def _presentation_finished(self, result: asyncio.Future) -> None:
        try:
            result.result()
        except asyncio.CancelledError:
            pass
        except Exception as error:
            self._handle_exception(error)

    async def _stage_selected(
        self, mode: str, participant_id: str | None, unmanaged: str | None
    ) -> None:
        if not self._view_active:
            return
        if mode == "toggle" or not self._target_is_staged(participant_id, unmanaged):
            result: StageResult | None
            if participant_id is None:
                if unmanaged is None:
                    self.notify("nothing to stage", severity="warning")
                    return
                result = await self._staging.stage_unmanaged(unmanaged)
            else:
                result = await self.stage_participant(participant_id)
            self._show_stage_result(result)
            if mode != "open" or result is None or result.outcome is not StageOutcome.STAGED:
                return
        result = await self._staging.focus()
        if not self._view_active:
            return
        if result.outcome is StageOutcome.FOCUSED:
            self._set_status("staged terminal focused")
        else:
            self.notify(result.reason or "no terminal is staged", severity="warning")

    def _target_is_staged(self, participant_id: str | None, pane_id: str | None) -> bool:
        projection = self._state.projection
        if participant_id is None:
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
        if not self._view_active:
            return
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
