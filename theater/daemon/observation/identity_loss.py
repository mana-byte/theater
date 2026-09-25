"""Source-error and transcript identity-loss wiring over the ``FailureTracker``."""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any

from theater.constants.observation import CORRELATION_AMBIGUOUS_CODE
from theater.daemon.observation.attachment import AttachmentManager
from theater.daemon.observation.failures import FailureTracker
from theater.daemon.observation.live import LiveRegistration
from theater.harness.source import Batch, IdentityLossEvidence

logger = logging.getLogger("theater.observer")


class IdentityLossWiring:
    if TYPE_CHECKING:
        _attachments: AttachmentManager
        _failures: FailureTracker
        _finish: Callable[..., Any]

    def transcript_correlation_ambiguous(self, pid: str) -> bool:
        """Expose current attribution failure before another send creates a job."""
        return self._failures.has_source_error(pid, CORRELATION_AMBIGUOUS_CODE)

    def transcript_identity_lost(self, pid: str) -> bool:
        return self._failures.transcript_identity_lost(pid)

    def _restore_transcript_identity_loss(self, pid: str) -> None:
        self._failures.restore_transcript_identity_loss(pid, finish_fn=self._finish)

    def _sweep_identity_lost_grace(
        self,
        pid: str,
        failed_at: float | None = None,
        *,
        registration: LiveRegistration | None = None,
    ) -> None:
        self._failures.sweep_identity_lost_grace(
            pid,
            failed_at,
            finish_fn=partial(self._finish, registration=registration),
        )

    def mark_transcript_identity_lost(
        self,
        pid: str,
        reason: str,
        *,
        registration: LiveRegistration | None = None,
    ) -> None:
        self._failures.mark_transcript_identity_lost(
            pid,
            reason,
            finish_fn=partial(self._finish, registration=registration),
        )

    def _handle_source_error(
        self,
        pid: str,
        batch: Batch,
        *,
        registration: LiveRegistration | None = None,
    ) -> None:
        self._failures.handle_source_error(
            pid,
            batch,
            finish_fn=partial(self._finish, registration=registration),
        )

    def _update_source_error(self, pid: str, batch: Batch) -> None:
        self._failures.update_source_error(pid, batch, finish_fn=self._finish)

    def _clear_source_error_on_progress(self, pid: str, batch: Batch) -> None:
        self._failures.clear_source_error_on_progress(pid, batch)

    def _evidence_is_bound_to_another_live_participant(
        self, pid: str, evidence: IdentityLossEvidence
    ) -> bool:
        return self._failures.evidence_is_bound_to_another_live(
            pid,
            evidence,
            bound_transcripts=self._attachments._bound_transcripts,
            binding_sessions=self._attachments._binding_sessions,
        )

    def _location_bound_to_another_live(self, pid: str, location: str) -> bool:
        return self._failures._location_bound_to_another_live(
            pid, location, self._attachments._bound_transcripts
        )

    def _session_id_bound_to_another_live(self, pid: str, session_id: str | None) -> bool:
        return self._failures._session_id_bound_to_another_live(
            pid,
            session_id,
            self._attachments._bound_transcripts,
            self._attachments._binding_sessions,
        )

    def _confirm_identity_loss(self, pid: str, evidence: IdentityLossEvidence) -> bool:
        return self._failures.confirm_identity_loss(pid, evidence)

    def _reset_identity_loss_confirmation(self, pid: str) -> None:
        self._failures.reset_identity_loss_confirmation(pid)
