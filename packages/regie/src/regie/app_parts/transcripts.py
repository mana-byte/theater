"""Transcript identity recovery: the candidates the daemon admits, and binding one."""

from __future__ import annotations

from regie.app_parts._shared import _AppBase
from regie.app_parts.projection import attempt_snapshot
from regie.controllers.transcripts import TranscriptBindState
from theater.frontend import (
    FrontendClientError,
    FrontendResponseError,
    FrontendTransportError,
    TranscriptCandidate,
)


class TranscriptRecovery(_AppBase):
    def transcript_recovery_available(self, participant_id: str) -> bool:
        projection = self._state.projection
        participant = None if projection is None else projection.participants.get(participant_id)
        identity = None if participant is None else participant.transcript_identity
        return participant is not None and (identity is None or identity.state != "trusted")

    async def load_transcript_candidates(
        self, participant_id: str
    ) -> tuple[TranscriptCandidate, ...]:
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
        participant_id: str,
    ) -> None:
        projection = self._state.projection
        if projection is None or participant_id not in projection.participants:
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
        if prior_owner_id is not None:
            # Taking a transcript from another participant is rare and destructive: CLI only.
            self.notify(
                f"this transcript belongs to {prior_owner_id}; transfer it with "
                f"`theater bind {participant_id} {candidate.location} "
                f"--transfer-from {prior_owner_id}`",
                severity="warning",
            )
            return
        self._start_transcript_bind(participant_id, candidate)

    def _start_transcript_bind(self, participant_id: str, candidate: TranscriptCandidate) -> None:
        self.run_worker(
            self._bind_transcript_candidate(participant_id, candidate.location),
            exclusive=False,
        )

    async def _bind_transcript_candidate(self, participant_id: str, location: str) -> None:
        record = await self._transcript_bindings.bind(participant_id, location, prior_owner_id=None)
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
        attempt = await attempt_snapshot(self._state)
        if isinstance(attempt, Exception):
            self._show_state_error(attempt)
            return
        projection = attempt
        self._last_state_error = None
        await self._refresh_unmanaged(projection, force=True)
        self._show_projection(projection)
        if self._surface.trajectory_participant_id == participant_id:
            try:
                await self._trajectory.retry(participant_id)
            except Exception as exc:
                self.notify(f"trajectory refresh failed: {exc}", severity="warning")
        self._restore_tree_focus()
