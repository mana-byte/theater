"""Session control prompts and their public-capability submissions."""

from __future__ import annotations

from regie.app_parts._shared import _AppBase
from regie.controllers.actions import ActionRecord
from regie.controllers.controls import format_controls_report
from regie.controllers.staging import StageOutcome
from regie.resume import ResumeCandidate
from regie.ui_constants import REGIE_CONTROLS_REPORT_TIMEOUT_SECONDS
from regie.widgets.prompts import (
    ControlPromptScreen,
    SettingsPromptScreen,
)
from theater.frontend import (
    FrontendClientError,
    FrontendResponseError,
    FrontendTransportError,
)


class ControlActions(_AppBase):
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

    def action_send(self) -> None:
        self._prompt_control("Send prompt", "message to deliver", self.submit_send)

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

    async def submit_send(self, participant_id: str, prompt: str) -> ActionRecord:
        if refusal := self._control_refusal(participant_id, "send"):
            return refusal
        return await self._actions.send(participant_id, prompt)

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
