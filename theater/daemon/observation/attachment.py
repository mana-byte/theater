"""Transcript ownership, receipt staging, and attachment admission.

``AttachmentManager`` owns _bound_transcripts, _binding_correlation,
_binding_sessions, _sources, _receipt_candidates, _reset_watch_state and the
complete attachment transaction: commit/discard + collision/receipt admission
+ ownership update + persistence + bus/error clearing as one operation.
"""

from __future__ import annotations

import contextlib
import logging

from theater.constants.observation import CORRELATION_AMBIGUOUS_CODE
from theater.daemon.observation.identity import has_cwd_competitor, trusted_dead_owner_blocks
from theater.daemon.registry import Registry
from theater.harness.source import Attachment, Batch, Source, SourceContractError
from theater.models import Status
from theater.provenance import (
    TranscriptProvenance,
    is_trusted_provenance,
    normalize_provenance,
    provenance_at_least,
)
from theater.resume_floor import (
    decode_floor,
    floor_authorises_completion,
    floor_is_present,
)
from theater.transcript_identity import canonical_location, same_location

logger = logging.getLogger("theater.observer")


class AttachmentManager:
    """Owns the live collision table, source registry, and attachment transaction."""

    def __init__(self, store, registry: Registry, *, timing_fn):
        self.store = store
        self.registry = registry
        self._timing_fn = timing_fn
        self._bound_transcripts: dict[str, str] = {}
        self._binding_correlation: dict[str, str] = {}
        self._binding_sessions: dict[str, str | None] = {}
        self._sources: dict[str, Source] = {}
        self._receipt_candidates: dict[str, tuple[str, str]] = {}
        self._reset_watch_state: set[str] = set()

    def accept_attachment(
        self,
        pid: str,
        source: Source,
        batch: Batch,
        *,
        handle_source_error_fn,
        on_attach_fn,
        clear_source_errors_fn,
    ) -> bool:
        """Accept or reject a staged source attachment in one central place.

        The source has not changed its live cursor yet. Collision refusal can
        discard the candidate without losing the participant's own accepted
        transcript. A failure before commit/discard also discards the candidate.
        """
        attached = batch.attached
        if attached is None:
            return True
        decided = False
        try:
            if attached.correlation == str(TranscriptProvenance.HEURISTIC) and has_cwd_competitor(
                pid, attached.collision_domain, self.store, self.registry, self._sources
            ):
                participant = self.store.get_participant(pid)
                logger.warning(
                    "refusing heuristic transcript %s for %s: another live %s "
                    "participant shares its cwd",
                    attached.location,
                    pid,
                    participant.harness if participant is not None else "unknown",
                )
                source.discard_attachment()
                decided = True
                self._handle_attachment_ambiguity(pid, attached, handle_source_error_fn)
                return False
            if not is_trusted_provenance(attached.correlation):
                logger.warning(
                    "quarantining heuristic transcript %s for %s: cwd/time is not "
                    "trusted participant identity",
                    attached.location,
                    pid,
                )
                source.discard_attachment()
                decided = True
                self._handle_attachment_ambiguity(pid, attached, handle_source_error_fn)
                return False
            if trusted_dead_owner_blocks(pid, attached, self.store, self.registry):
                source.discard_attachment()
                decided = True
                self._handle_attachment_ambiguity(pid, attached, handle_source_error_fn)
                return False
            owner = self._bound_transcripts.get(canonical_location(attached.location))
            if owner is not None and owner != pid:
                holder = self.store.get_participant(owner)
                if holder is not None and holder.status is not Status.DEAD:
                    bound_loc = canonical_location(attached.location)
                    prior = self._binding_correlation.get(
                        bound_loc, str(TranscriptProvenance.EXACT)
                    )
                    if is_trusted_provenance(attached.correlation) and not (
                        is_trusted_provenance(prior)
                    ):
                        self._revoke_binding(attached.location, owner)
                    else:
                        logger.warning(
                            "transcript %s is already bound to %s (%s); refusing "
                            "to bind it to %s (%s)",
                            attached.location,
                            owner,
                            prior,
                            pid,
                            attached.correlation,
                        )
                        source.discard_attachment()
                        decided = True
                        self._handle_attachment_ambiguity(pid, attached, handle_source_error_fn)
                        return False
            source.commit_attachment()
            decided = True
        except Exception:
            if not decided:
                with contextlib.suppress(Exception):
                    source.discard_attachment()
            raise
        on_attach_fn(pid, attached)
        clear_source_errors_fn(pid, include_identity_lost=True)
        return True

    def _handle_attachment_ambiguity(
        self, pid: str, attached: Attachment, handle_source_error_fn
    ) -> None:
        """A refused rotation leaves an accepted source intact; only an
        initially unbound participant is unable to make progress."""
        if pid in self._bound_transcripts.values():
            return
        handle_source_error_fn(
            pid,
            Batch(
                error_code=CORRELATION_AMBIGUOUS_CODE,
                error=(
                    f"transcript candidate {attached.location!r} is not uniquely attributable "
                    "to this participant"
                ),
            ),
        )

    def _revoke_binding(self, location: str, owner: str) -> None:
        """Let exact process evidence displace an earlier cwd guess."""
        source = self._sources.get(owner)
        if source is None:
            raise SourceContractError(
                f"cannot revoke heuristic binding {location!r}: owner source is unavailable"
            )
        source.revoke_attachment()
        self._reset_watch_state.add(owner)
        loc = canonical_location(location)
        participant = self.store.get_participant(owner)
        bound_session = self._binding_sessions.get(loc)
        if participant is not None:
            if participant.session_id == bound_session:
                participant.session_id = None
                participant.session_correlation = None
            participant.transcript_location = None
            self.store.upsert_participant(participant)
        self._bound_transcripts.pop(loc, None)
        self._binding_correlation.pop(loc, None)
        self._binding_sessions.pop(loc, None)
        self.store.bus_append(
            "agent.observation_error",
            to_id=owner,
            payload={
                "code": "transcript_binding_revoked",
                "message": "an exact process claim displaced this heuristic transcript binding",
            },
        )
        logger.warning(
            "exact transcript claim revoked heuristic binding %s from %s", location, owner
        )

    def on_attach(
        self,
        pid: str,
        attached: Attachment,
        *,
        settle_fn,
        settle_from_event_fn,
        answer_turn_fn,
        turn_result_fn,
    ) -> None:
        """Record the effects of an attachment already accepted and committed."""
        self.release_transcript(pid)
        loc = canonical_location(attached.location)
        self._bound_transcripts[loc] = pid
        self._binding_correlation[loc] = attached.correlation
        self._binding_sessions[loc] = attached.session_id
        p = self.store.get_participant(pid)
        session_id = attached.session_id
        if p is not None:
            changed = False
            if p.transcript_location != loc:
                p.transcript_location = loc
                changed = True
            prior = normalize_provenance(p.session_correlation)
            incoming = normalize_provenance(attached.correlation)
            can_update_identity = is_trusted_provenance(incoming) and (
                not is_trusted_provenance(prior) or provenance_at_least(incoming, prior)
            )
            if session_id and p.session_id != session_id and can_update_identity:
                self._timing_fn("observer.attach", pid, p.created_at, harness=p.harness)
                p.session_id = session_id
                p.session_correlation = attached.correlation
                changed = True
            elif (
                session_id and p.session_correlation != attached.correlation and can_update_identity
            ):
                p.session_correlation = attached.correlation
                changed = True
            if changed:
                self.store.upsert_participant(p)
        self.store.bus_append(
            "agent.transcript",
            to_id=pid,
            payload={"path": attached.location, "skipped_records": attached.skipped},
        )
        logger.info(
            "observing %s at %s (+%d existing records)",
            pid,
            attached.location,
            attached.skipped,
        )
        floor_raw = p.resume_floor if p is not None else None
        if attached.last_event is not None and not floor_is_present(floor_raw):
            settle_from_event_fn(pid, attached.last_event)
        elif attached.last_event is not None and floor_is_present(floor_raw):
            floor = decode_floor(floor_raw)
            if floor_authorises_completion(floor, floor_raw=floor_raw, point=attached.point):
                settle_from_event_fn(pid, attached.last_event)
                self.store.clear_resume_floor(pid)
            else:
                logger.info(
                    "resume floor suppresses attach-derived status for %s (floor=%s, point=%s)",
                    pid,
                    floor_raw,
                    attached.point,
                )

    def release_transcript(self, pid: str) -> None:
        """Drop a participant's claim on its transcript, if it still holds it."""
        to_drop = [path for path, owner in self._bound_transcripts.items() if owner == pid]
        for path in to_drop:
            del self._bound_transcripts[path]
            self._binding_correlation.pop(path, None)
            self._binding_sessions.pop(path, None)

    def is_untrusted_rotation(self, pid: str, attached: Attachment) -> bool:
        participant = self.store.get_participant(pid)
        return (
            participant is not None
            and participant.transcript_location is not None
            and not same_location(participant.transcript_location, attached.location)
            and is_trusted_provenance(participant.session_correlation)
            and not is_trusted_provenance(attached.correlation)
        )

    def register_source(self, pid: str, source: Source, *, clear_source_errors_fn) -> None:
        self._sources[pid] = source
        self.stage_pending_receipt(pid, source, clear_source_errors_fn=clear_source_errors_fn)

    def transcript_receipt(
        self, pid: str, *, location: str, session_id: str, clear_source_errors_fn
    ) -> str:
        """Stage exact receipt evidence without persisting it before admission."""
        source = self._sources.get(pid)
        if source is None:
            self._receipt_candidates[pid] = (location, session_id)
            return "staged"
        return self._stage_receipt_source(
            pid,
            source,
            location=location,
            session_id=session_id,
            clear_source_errors_fn=clear_source_errors_fn,
        )

    def stage_pending_receipt(self, pid: str, source: Source, *, clear_source_errors_fn) -> None:
        candidate = self._receipt_candidates.pop(pid, None)
        if candidate is None:
            return
        location, session_id = candidate
        self._stage_receipt_source(
            pid,
            source,
            location=location,
            session_id=session_id,
            clear_source_errors_fn=clear_source_errors_fn,
        )

    def _stage_receipt_source(
        self, pid: str, source: Source, *, location: str, session_id: str, clear_source_errors_fn
    ) -> str:
        result = source.admit_exact_location(location=location, session_id=session_id)
        if result not in ("accepted", "staged"):
            raise SourceContractError(
                f"{type(source).__name__}.admit_exact_location() must return 'accepted' or "
                f"'staged' (the ReceiptAdmission literal), got {result!r}. A source that "
                "cannot admit a receipt should raise rather than return None or another value, "
                "because a silent non-admission tells the caller the receipt worked while "
                "nothing is persisted."
            )
        if result == "accepted":
            loc = canonical_location(location)
            self.store.record_transcript_receipt(
                pid,
                session_id=session_id,
                transcript_location=loc,
            )
            self._binding_correlation[loc] = str(TranscriptProvenance.EXACT)
            self._binding_sessions[loc] = session_id
        if result in {"accepted", "staged"}:
            clear_source_errors_fn(pid, include_identity_lost=True)
        return result

    def record_operator_binding(
        self,
        pid: str,
        location: str,
        session_id: str | None,
        *,
        prior_owner: str | None,
        clear_source_errors_fn,
    ) -> None:
        """Mirror an accepted operator binding in the live collision table."""
        loc = canonical_location(location)
        if prior_owner is not None:
            self.release_transcript(prior_owner)
            clear_source_errors_fn(prior_owner, include_identity_lost=True)
        self.release_transcript(pid)
        self._bound_transcripts[loc] = pid
        self._binding_correlation[loc] = str(TranscriptProvenance.OPERATOR)
        self._binding_sessions[loc] = session_id
