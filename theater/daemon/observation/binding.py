"""Transcript binding: attachment acceptance, operator binds, and transcript receipts."""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any

from theater.daemon.observation.attachment import AttachmentManager
from theater.daemon.observation.failures import FailureTracker
from theater.daemon.observation.identity import history_correlation_is_ambiguous
from theater.daemon.observation.live import LiveRegistration
from theater.daemon.registry import Registry
from theater.harness.source import Attachment, Batch, History, Source

if TYPE_CHECKING:
    from theater.daemon.store import Store

logger = logging.getLogger("theater.observer")


class TranscriptBinding:
    if TYPE_CHECKING:
        _attachments: AttachmentManager
        _failures: FailureTracker
        _pending_transcripts: set[str]
        registry: Registry
        store: Store
        _answer_turn: Callable[..., Any]
        _handle_source_error: Callable[..., Any]
        _settle: Callable[..., Any]
        _settle_from_event: Callable[..., Any]
        _turn_result: Callable[..., Any]

    def transcript_pending(self, pid: str) -> bool:
        """A source is waiting for its first transcript, not reporting an identity conflict."""
        return pid in self._pending_transcripts

    def record_operator_binding(
        self,
        pid: str,
        location: str,
        session_id: str | None,
        *,
        prior_owner: str | None = None,
    ) -> None:
        self._attachments.record_operator_binding(
            pid,
            location,
            session_id,
            prior_owner=prior_owner,
            clear_source_errors_fn=self._failures.clear_source_errors,
        )

    def _is_untrusted_rotation(self, pid: str, attached: Attachment) -> bool:
        return self._attachments.is_untrusted_rotation(pid, attached)

    def _accept_attachment(
        self,
        pid: str,
        source: Source,
        batch: Batch,
        *,
        registration: LiveRegistration | None = None,
    ) -> bool:
        return self._attachments.accept_attachment(
            pid,
            source,
            batch,
            handle_source_error_fn=partial(self._handle_source_error, registration=registration),
            on_attach_fn=partial(self._on_attach, registration=registration),
            clear_source_errors_fn=self._failures.clear_source_errors,
        )

    def _handle_attachment_ambiguity(self, pid: str, attached: Attachment) -> None:
        self._attachments._handle_attachment_ambiguity(pid, attached, self._handle_source_error)

    def _revoke_binding(self, location: str, owner: str) -> None:
        self._attachments._revoke_binding(location, owner)

    def _has_cwd_competitor(self, pid: str, collision_domain: str | None) -> bool:
        from theater.daemon.observation.identity import has_cwd_competitor

        return has_cwd_competitor(
            pid, collision_domain, self.store, self.registry, self._attachments._sources
        )

    def _trusted_dead_owner_blocks(self, pid: str, attached: Attachment) -> bool:
        from theater.daemon.observation.identity import trusted_dead_owner_blocks

        return trusted_dead_owner_blocks(pid, attached, self.store, self.registry)

    def history_is_ambiguous(self, pid: str, history: History) -> bool:
        return history_correlation_is_ambiguous(self.registry, pid, history)

    def transcript_receipt(self, pid: str, *, location: str, session_id: str) -> str:
        return self._attachments.transcript_receipt(
            pid,
            location=location,
            session_id=session_id,
            clear_source_errors_fn=self._failures.clear_source_errors,
        )

    def _stage_receipt_source(
        self, pid: str, source: Source, *, location: str, session_id: str
    ) -> str:
        return self._attachments._stage_receipt_source(
            pid,
            source,
            location=location,
            session_id=session_id,
            clear_source_errors_fn=self._failures.clear_source_errors,
        )

    def _on_attach(
        self,
        pid: str,
        attached: Attachment,
        *,
        registration: LiveRegistration | None = None,
    ) -> None:
        self._attachments.on_attach(
            pid,
            attached,
            settle_fn=self._settle,
            settle_from_event_fn=partial(self._settle_from_event, registration=registration),
            answer_turn_fn=partial(self._answer_turn, registration=registration),
            turn_result_fn=self._turn_result,
        )
