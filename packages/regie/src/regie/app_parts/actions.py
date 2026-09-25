"""Action lifecycle: observe, render, and reconcile completed operations."""

from __future__ import annotations

from functools import partial
from time import monotonic

from regie.app_parts._shared import _AppBase, logger
from regie.controllers.actions import ActionRecord
from regie.controllers.controls import describe_action
from regie.latency import action_phase
from regie.widgets import ParticipantTree
from theater.frontend import (
    FrontendClientError,
    FrontendResponseError,
    FrontendTransportError,
    StateProjection,
    StateSynchronizationError,
)


class ActionTracking(_AppBase):
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
        if not self._view_active:
            return
        self._action_presentation.retain(self._actions.records)
        for record in self._actions.records:
            changed = self._action_presentation.changed(record)
            if changed or self._action_presentation.needs_reconciliation(record):
                self._show_action(record, announce=changed)

    def _show_action(self, record: ActionRecord, *, announce: bool = True) -> None:
        message, severity = describe_action(record)
        self._set_status(message)
        changed = self._action_presentation.presented(record)
        if announce and changed and severity in {"warning", "error"}:
            self.notify(message, severity=severity)
        if self._action_presentation.begin_reconciliation(record):
            self.run_worker(self._reconcile_completed_action(record), exclusive=False)
        elif not self._action_presentation.reconciling(record):
            self._actions.acknowledge(record)

    async def _reconcile_completed_action(self, record: ActionRecord) -> None:
        succeeded = False
        try:
            if not self._view_active:
                return
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
            if not self._view_active:
                return
            # Render the fresh daemon state first; local pane discovery runs `ps`
            # and only decorates the tree, so it must not delay the visible result.
            with action_phase(record, "projection"):
                self._show_projection(self._state.projection or projection)
                self.set_focus(None)
                self.query_one(ParticipantTree).set_cursor_visible(True)
            succeeded = True
            self.call_after_refresh(self._record_action_rendered, record, monotonic())
            self.run_worker(self._refresh_unmanaged_after(record, projection), exclusive=False)
            if record.action == "spawn" and participant_id is not None:
                self._present_spawned(participant_id)
        finally:
            self._action_presentation.finish_reconciliation(record, succeeded=succeeded)

    def _present_spawned(self, participant_id: str) -> None:
        """A participant spawned from Régie opens staged and focused, ready for input."""
        self.select_participant(participant_id)
        self._submit_presentation(
            "open", partial(self._stage_selected, "open", participant_id, None)
        )

    async def _refresh_unmanaged_after(
        self, record: ActionRecord, projection: StateProjection
    ) -> None:
        with action_phase(record, "unmanaged"):
            await self._refresh_unmanaged(projection, force=True)
        if self._view_active and (current := self._state.projection) is not None:
            self._show_projection(current)

    def _record_action_rendered(self, record: ActionRecord, projected_at: float) -> None:
        self._actions.acknowledge(record)
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
