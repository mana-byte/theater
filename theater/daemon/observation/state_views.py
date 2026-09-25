"""Legacy instance-state properties forwarding to collaborators (kept for tests)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from theater.daemon.observation.attachment import AttachmentManager
from theater.daemon.observation.completion import CompletionTracker
from theater.daemon.observation.failures import FailureTracker
from theater.harness.source import Source

logger = logging.getLogger("theater.observer")


class CollaboratorStateViews:
    if TYPE_CHECKING:
        _attachments: AttachmentManager
        _completion: CompletionTracker
        _failures: FailureTracker

    @property
    def _unmatched(self) -> dict[str, int]:
        return self._completion._unmatched

    @_unmatched.setter
    def _unmatched(self, value: dict[str, int]) -> None:
        self._completion._unmatched = value

    @property
    def _source_errors(self) -> dict:
        return self._failures._source_errors

    @_source_errors.setter
    def _source_errors(self, value) -> None:
        self._failures._source_errors = value

    @property
    def _identity_lost(self) -> set[str]:
        return self._failures._identity_lost

    @_identity_lost.setter
    def _identity_lost(self, value: set[str]) -> None:
        self._failures._identity_lost = value

    @property
    def _identity_loss_replayed(self) -> set[str]:
        return self._failures._identity_loss_replayed

    @_identity_loss_replayed.setter
    def _identity_loss_replayed(self, value: set[str]) -> None:
        self._failures._identity_loss_replayed = value

    @property
    def _identity_loss_pending(self) -> dict:
        return self._failures._identity_loss_pending

    @_identity_loss_pending.setter
    def _identity_loss_pending(self, value: dict) -> None:
        self._failures._identity_loss_pending = value

    @property
    def _bound_transcripts(self) -> dict[str, str]:
        return self._attachments._bound_transcripts

    @_bound_transcripts.setter
    def _bound_transcripts(self, value: dict[str, str]) -> None:
        self._attachments._bound_transcripts = value

    @property
    def _binding_correlation(self) -> dict[str, str]:
        return self._attachments._binding_correlation

    @_binding_correlation.setter
    def _binding_correlation(self, value: dict[str, str]) -> None:
        self._attachments._binding_correlation = value

    @property
    def _binding_sessions(self) -> dict[str, str | None]:
        return self._attachments._binding_sessions

    @_binding_sessions.setter
    def _binding_sessions(self, value: dict[str, str | None]) -> None:
        self._attachments._binding_sessions = value

    @property
    def _sources(self) -> dict[str, Source]:
        return self._attachments._sources

    @_sources.setter
    def _sources(self, value: dict[str, Source]) -> None:
        self._attachments._sources = value

    @property
    def _receipt_candidates(self) -> dict[str, tuple[str, str]]:
        return self._attachments._receipt_candidates

    @_receipt_candidates.setter
    def _receipt_candidates(self, value: dict[str, tuple[str, str]]) -> None:
        self._attachments._receipt_candidates = value

    @property
    def _reset_watch_state(self) -> set[str]:
        return self._attachments._reset_watch_state

    @_reset_watch_state.setter
    def _reset_watch_state(self, value: set[str]) -> None:
        self._attachments._reset_watch_state = value
