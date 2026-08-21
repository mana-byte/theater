"""Source errors, quarantine, and identity-loss grace.

``FailureTracker`` owns _source_errors, _identity_lost,
_identity_loss_replayed, _identity_loss_pending and the grace window,
bus error publication, and crash-vs-quarantine decision.
"""

from __future__ import annotations

import logging

from theater.constants.observation import (
    IDENTITY_LOSS_CONFIRMATIONS,
)
from theater.models import JobState, Status
from theater.transcript_identity import (
    TRANSCRIPT_IDENTITY_LOST_CODE,
    canonical_location,
    same_location,
    transcript_identity_recovery_message,
)

logger = logging.getLogger("theater.observer")


class FailureTracker:
    """Owns source-error bookkeeping, quarantine state, and identity-loss grace."""

    def __init__(self, store, registry, *, wall_now_fn, grace_fn, jobs_fn):
        self.store = store
        self.registry = registry
        self._wall_now_fn = wall_now_fn
        self._grace_fn = grace_fn
        self._jobs_fn = jobs_fn
        self._source_errors: dict[tuple[str, str], float] = {}
        self._identity_lost: set[str] = set()
        self._identity_loss_replayed: set[str] = set()
        self._identity_loss_pending: dict[str, tuple[str, int]] = {}

    @property
    def jobs(self):
        return self._jobs_fn()

    def handle_source_error(self, pid: str, batch, *, finish_fn) -> None:
        """Report broken exact correlation and bound affected awaits."""
        assert batch.error_code is not None
        key = (pid, batch.error_code)
        identity_was_active = pid in self._identity_lost
        if batch.error_code == TRANSCRIPT_IDENTITY_LOST_CODE:
            participant = self.store.get_participant(pid)
            if participant is None or participant.status is Status.DEAD:
                return
            self._identity_lost.add(pid)
        for stale in [item for item in self._source_errors if item[0] == pid and item != key]:
            self._source_errors.pop(stale, None)
        failed_at = self._source_errors.get(key)
        if failed_at is None:
            failed_at = self._wall_now_fn()
            self._source_errors[key] = failed_at
            logger.error("observation failed for %s: %s", pid, batch.error or batch.error_code)
            if not (batch.error_code == TRANSCRIPT_IDENTITY_LOST_CODE and identity_was_active):
                self.store.bus_append(
                    "agent.observation_error",
                    to_id=pid,
                    payload={"code": batch.error_code, "message": batch.error or ""},
                )
        if self.jobs is None:
            return
        if batch.error_code == TRANSCRIPT_IDENTITY_LOST_CODE:
            self.sweep_identity_lost_grace(pid, failed_at, finish_fn=finish_fn)
            return
        now = self._wall_now_fn()
        grace = self._grace_fn()
        for job in self.store.running_jobs_for_target(pid):
            if now - max(failed_at, job.created_at) >= grace:
                finish_fn(
                    job.handle,
                    "",
                    error_code=batch.error_code,
                    state=JobState.CRASHED,
                    raw_result=None,
                )

    def sweep_identity_lost_grace(self, pid: str, failed_at: float | None, *, finish_fn) -> None:
        """Re-evaluate running jobs against the identity-loss grace window."""
        if self.jobs is None:
            return
        key = (pid, TRANSCRIPT_IDENTITY_LOST_CODE)
        if failed_at is None:
            failed_at = self._source_errors.get(key)
        if failed_at is None:
            return
        now = self._wall_now_fn()
        grace = self._grace_fn()
        for job in self.store.running_jobs_for_target(pid):
            if now - max(failed_at, job.created_at) >= grace:
                finish_fn(
                    job.handle,
                    transcript_identity_recovery_message(pid),
                    error_code=TRANSCRIPT_IDENTITY_LOST_CODE,
                    state=JobState.CRASHED,
                    raw_result=None,
                )

    def restore_transcript_identity_loss(self, pid: str, *, finish_fn) -> None:
        """Replay retained audit once for this watcher lifecycle."""
        if pid in self._identity_loss_replayed:
            return
        self._identity_loss_replayed.add(pid)
        participant = self.store.get_participant(pid)
        if participant is None or participant.status is Status.DEAD:
            return
        if not self.store.observation_error_active(pid, TRANSCRIPT_IDENTITY_LOST_CODE):
            return
        self._identity_lost.add(pid)
        persisted_ts = self.store.observation_error_timestamp(pid, TRANSCRIPT_IDENTITY_LOST_CODE)
        failed_at = persisted_ts if persisted_ts is not None else self._wall_now_fn()
        self._source_errors[(pid, TRANSCRIPT_IDENTITY_LOST_CODE)] = failed_at
        self.sweep_identity_lost_grace(pid, failed_at, finish_fn=finish_fn)

    def update_source_error(self, pid: str, batch, *, finish_fn) -> None:
        if batch.error_code is None:
            self.clear_source_errors(pid)
            return
        self.handle_source_error(pid, batch, finish_fn=finish_fn)

    def report_source_error(self, pid: str, batch, *, finish_fn) -> None:
        if batch.error_code is not None:
            self.handle_source_error(pid, batch, finish_fn=finish_fn)

    def clear_source_error_on_progress(self, pid: str, batch) -> None:
        """Clear source errors on actual source progress, not a clean empty poll."""
        if batch.error_code is None:
            self.clear_source_errors(pid)
            if batch.progressed or bool(batch.events) or batch.attached is not None:
                self.reset_identity_loss_confirmation(pid)

    def clear_source_errors(self, pid: str, *, include_identity_lost: bool = False) -> None:
        for key in [item for item in self._source_errors if item[0] == pid]:
            if key[1] == TRANSCRIPT_IDENTITY_LOST_CODE and not include_identity_lost:
                continue
            self._source_errors.pop(key, None)
        if include_identity_lost:
            self._identity_lost.discard(pid)
            self._identity_loss_pending.pop(pid, None)

    def confirm_identity_loss(self, pid: str, evidence) -> bool:
        """Require consecutive relocate windows with the same evidence location."""
        pending = self._identity_loss_pending.get(pid)
        canonical = canonical_location(evidence.location)
        count = (
            pending[1] + 1
            if pending is not None and same_location(pending[0], evidence.location)
            else 1
        )
        self._identity_loss_pending[pid] = (canonical, count)
        return count >= IDENTITY_LOSS_CONFIRMATIONS

    def reset_identity_loss_confirmation(self, pid: str) -> None:
        """Semantic progress on the pinned source resets confirmation."""
        self._identity_loss_pending.pop(pid, None)

    def transcript_identity_lost(self, pid: str) -> bool:
        """Pure cached predicate; only the watch path may enter quarantine."""
        participant = self.store.get_participant(pid)
        if participant is None or participant.status is Status.DEAD:
            return False
        return pid in self._identity_lost

    def mark_transcript_identity_lost(self, pid: str, reason: str, *, finish_fn) -> None:
        """Enter quarantine from positive evidence in the observation path."""
        participant = self.store.get_participant(pid)
        if participant is None or participant.status is Status.DEAD:
            return
        self.handle_source_error(
            pid,
            Batch(
                waiting=True,
                error_code=TRANSCRIPT_IDENTITY_LOST_CODE,
                error=transcript_identity_recovery_message(pid, reason),
            ),
            finish_fn=finish_fn,
        )

    def evidence_is_bound_to_another_live(
        self, pid: str, evidence, *, bound_transcripts, binding_sessions
    ) -> bool:
        """Whether loss evidence names a transcript another live participant owns."""
        if self._location_bound_to_another_live(pid, evidence.location, bound_transcripts):
            return True
        return self._session_id_bound_to_another_live(
            pid, evidence.session_id, bound_transcripts, binding_sessions
        )

    def _location_bound_to_another_live(self, pid: str, location: str, bound_transcripts) -> bool:
        owner = bound_transcripts.get(canonical_location(location))
        if owner is not None and owner != pid:
            holder = self.store.get_participant(owner)
            if holder is not None and holder.status is not Status.DEAD:
                return True
        for other in self.registry.list():
            if other.id == pid or other.status is Status.DEAD:
                continue
            if same_location(other.transcript_location, location):
                return True
        return False

    def _session_id_bound_to_another_live(
        self, pid: str, session_id: str | None, bound_transcripts, binding_sessions
    ) -> bool:
        if session_id is None:
            return False
        for other in self.registry.list():
            if other.id == pid or other.status is Status.DEAD:
                continue
            if session_id == other.session_id and other.session_id is not None:
                return True
        for loc, sid in binding_sessions.items():
            if sid != session_id:
                continue
            bound_pid = bound_transcripts.get(loc)
            if bound_pid is not None and bound_pid != pid:
                holder = self.store.get_participant(bound_pid)
                if holder is not None and holder.status is not Status.DEAD:
                    return True
        return False


# Imported here so mark_transcript_identity_lost can construct a Batch.
from theater.harness.source import Batch  # noqa: E402
