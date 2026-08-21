"""Transcript ownership, receipt staging, and attachment admission.

The attachment transaction is one operation: commit/discard + collision/receipt
admission + ownership update + persistence + bus/error clearing. All of it
lives here so a failure before commit/discard discards the candidate without
losing the participant's own accepted transcript.
"""

from __future__ import annotations

import contextlib
import logging

from theater.constants.observation import CORRELATION_AMBIGUOUS_CODE
from theater.daemon.observation.identity import has_cwd_competitor, trusted_dead_owner_blocks
from theater.daemon.registry import Registry
from theater.harness.source import (
    Attachment,
    Batch,
    Source,
    SourceContractError,
)
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


def is_untrusted_rotation(pid: str, attached: Attachment, store) -> bool:
    """Whether a refresh-proposed attachment is an untrusted rotation."""
    participant = store.get_participant(pid)
    return (
        participant is not None
        and participant.transcript_location is not None
        and not same_location(participant.transcript_location, attached.location)
        and is_trusted_provenance(participant.session_correlation)
        and not is_trusted_provenance(attached.correlation)
    )


def accept_attachment(
    pid: str,
    source: Source,
    batch: Batch,
    *,
    store,
    registry: Registry,
    sources: dict,
    bound_transcripts: dict,
    binding_correlation: dict,
    binding_sessions: dict,
    handle_source_error_fn,
    on_attach_fn,
    clear_source_errors_fn,
) -> bool:
    """Accept or reject a staged source attachment in one central place.

    The source has not changed its live cursor yet. Collision refusal can
    therefore discard the candidate without losing the participant's own
    accepted transcript.
    """
    attached = batch.attached
    if attached is None:
        return True
    decided = False
    try:
        if attached.correlation == str(TranscriptProvenance.HEURISTIC) and has_cwd_competitor(
            pid, attached.collision_domain, store, registry, sources
        ):
            participant = store.get_participant(pid)
            logger.warning(
                "refusing heuristic transcript %s for %s: another live %s "
                "participant shares its cwd",
                attached.location,
                pid,
                participant.harness if participant is not None else "unknown",
            )
            source.discard_attachment()
            decided = True
            handle_attachment_ambiguity(
                pid,
                attached,
                bound_transcripts=bound_transcripts,
                handle_source_error_fn=handle_source_error_fn,
            )
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
            handle_attachment_ambiguity(
                pid,
                attached,
                bound_transcripts=bound_transcripts,
                handle_source_error_fn=handle_source_error_fn,
            )
            return False
        if trusted_dead_owner_blocks(pid, attached, store, registry):
            source.discard_attachment()
            decided = True
            handle_attachment_ambiguity(
                pid,
                attached,
                bound_transcripts=bound_transcripts,
                handle_source_error_fn=handle_source_error_fn,
            )
            return False
        owner = bound_transcripts.get(canonical_location(attached.location))
        if owner is not None and owner != pid:
            holder = store.get_participant(owner)
            if holder is not None and holder.status is not Status.DEAD:
                bound_loc = canonical_location(attached.location)
                prior = binding_correlation.get(bound_loc, str(TranscriptProvenance.EXACT))
                if is_trusted_provenance(attached.correlation) and not (
                    is_trusted_provenance(prior)
                ):
                    revoke_binding(
                        attached.location,
                        owner,
                        store=store,
                        sources=sources,
                        reset_watch_state=None,
                        bound_transcripts=bound_transcripts,
                        binding_correlation=binding_correlation,
                        binding_sessions=binding_sessions,
                    )
                else:
                    logger.warning(
                        "transcript %s is already bound to %s (%s); refusing to bind it to %s (%s)",
                        attached.location,
                        owner,
                        prior,
                        pid,
                        attached.correlation,
                    )
                    source.discard_attachment()
                    decided = True
                    handle_attachment_ambiguity(
                        pid,
                        attached,
                        bound_transcripts=bound_transcripts,
                        handle_source_error_fn=handle_source_error_fn,
                    )
                    return False
        source.commit_attachment()
        decided = True
    except Exception:
        if not decided:
            # Preserve the original failure if cleanup itself is broken.
            with contextlib.suppress(Exception):
                source.discard_attachment()
        raise
    on_attach_fn(pid, attached)
    clear_source_errors_fn(pid, include_identity_lost=True)
    return True


def handle_attachment_ambiguity(
    pid: str,
    attached: Attachment,
    *,
    bound_transcripts: dict,
    handle_source_error_fn,
) -> None:
    """A refused rotation leaves an accepted source intact; only an initially
    unbound participant is unable to make progress."""
    if pid in bound_transcripts.values():
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


def revoke_binding(
    location: str,
    owner: str,
    *,
    store,
    sources: dict,
    reset_watch_state,
    bound_transcripts: dict,
    binding_correlation: dict,
    binding_sessions: dict,
) -> None:
    """Let exact process evidence displace an earlier cwd guess."""
    source = sources.get(owner)
    if source is None:
        raise SourceContractError(
            f"cannot revoke heuristic binding {location!r}: owner source is unavailable"
        )
    source.revoke_attachment()
    if reset_watch_state is not None:
        reset_watch_state.add(owner)
    loc = canonical_location(location)
    participant = store.get_participant(owner)
    bound_session = binding_sessions.get(loc)
    if participant is not None:
        if participant.session_id == bound_session:
            participant.session_id = None
            participant.session_correlation = None
        participant.transcript_location = None
        store.upsert_participant(participant)
    bound_transcripts.pop(loc, None)
    binding_correlation.pop(loc, None)
    binding_sessions.pop(loc, None)
    store.bus_append(
        "agent.observation_error",
        to_id=owner,
        payload={
            "code": "transcript_binding_revoked",
            "message": "an exact process claim displaced this heuristic transcript binding",
        },
    )
    logger.warning("exact transcript claim revoked heuristic binding %s from %s", location, owner)


def on_attach(
    pid: str,
    attached: Attachment,
    *,
    store,
    registry: Registry,
    bound_transcripts: dict,
    binding_correlation: dict,
    binding_sessions: dict,
    release_transcript_fn,
    settle_fn,
    settle_from_event_fn,
    answer_turn_fn,
    turn_result_fn,
    timing_fn,
) -> None:
    """Record the effects of an attachment already accepted and committed."""

    # Release any previous binding this participant held.
    release_transcript_fn(pid)
    loc = canonical_location(attached.location)
    bound_transcripts[loc] = pid
    binding_correlation[loc] = attached.correlation
    binding_sessions[loc] = attached.session_id
    p = store.get_participant(pid)
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
            timing_fn("observer.attach", pid, p.created_at, harness=p.harness)
            p.session_id = session_id
            p.session_correlation = attached.correlation
            changed = True
        elif session_id and p.session_correlation != attached.correlation and can_update_identity:
            p.session_correlation = attached.correlation
            changed = True
        if changed:
            store.upsert_participant(p)
    store.bus_append(
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
    # Derive an initial status from the last record skipped.
    floor_raw = p.resume_floor if p is not None else None
    if attached.last_event is not None and not floor_is_present(floor_raw):
        settle_from_event_fn(pid, attached.last_event)
    elif attached.last_event is not None and floor_is_present(floor_raw):
        floor = decode_floor(floor_raw)
        if floor_authorises_completion(floor, floor_raw=floor_raw, point=attached.point):
            settle_from_event_fn(pid, attached.last_event)
            store.clear_resume_floor(pid)
        else:
            logger.info(
                "resume floor suppresses attach-derived status for %s (floor=%s, point=%s)",
                pid,
                floor_raw,
                attached.point,
            )


def release_transcript(
    pid: str, *, bound_transcripts: dict, binding_correlation: dict, binding_sessions: dict
) -> None:
    """Drop a participant's claim on its transcript, if it still holds it."""
    to_drop = [path for path, owner in bound_transcripts.items() if owner == pid]
    for path in to_drop:
        del bound_transcripts[path]
        binding_correlation.pop(path, None)
        binding_sessions.pop(path, None)


def stage_receipt_source(
    pid: str,
    source: Source,
    *,
    location: str,
    session_id: str,
    store,
    binding_correlation: dict,
    binding_sessions: dict,
    clear_source_errors_fn,
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
        store.record_transcript_receipt(
            pid,
            session_id=session_id,
            transcript_location=loc,
        )
        binding_correlation[loc] = str(TranscriptProvenance.EXACT)
        binding_sessions[loc] = session_id
    if result in {"accepted", "staged"}:
        # A staged exact receipt deliberately has not persisted ownership
        # yet, but it must re-arm the watcher so the reducer can inspect
        # and commit that exact attachment on its next read.
        clear_source_errors_fn(pid, include_identity_lost=True)
    return result


def record_operator_binding(
    pid: str,
    location: str,
    session_id: str | None,
    *,
    prior_owner: str | None,
    store,
    bound_transcripts: dict,
    binding_correlation: dict,
    binding_sessions: dict,
    release_transcript_fn,
    clear_source_errors_fn,
) -> None:
    """Mirror an accepted operator binding in the live collision table."""
    loc = canonical_location(location)
    if prior_owner is not None:
        release_transcript_fn(prior_owner)
        clear_source_errors_fn(prior_owner, include_identity_lost=True)
    release_transcript_fn(pid)
    bound_transcripts[loc] = pid
    binding_correlation[loc] = str(TranscriptProvenance.OPERATOR)
    binding_sessions[loc] = session_id
