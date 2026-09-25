"""Transcript identity recovery through the candidate palette."""

from __future__ import annotations

from textual.command import CommandPalette

from regie.app_parts._shared import _AppBase
from regie.controllers.transcripts import TranscriptBindState
from regie.palette import TranscriptCandidateCommands
from regie.widgets.prompts import TranscriptTransferScreen
from theater.frontend import (
    FrontendClientError,
    FrontendResponseError,
    FrontendTransportError,
    StateSynchronizationError,
    TranscriptCandidate,
)


class TranscriptRecovery(_AppBase):
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
