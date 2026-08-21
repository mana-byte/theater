"""Transcript RPC handlers: receipts, candidates, bind, read_transcript.

Also owns the transcript-ownership helpers (_reject_cross_participant_receipt,
_reject_unbound_same_cwd_receipt, _candidate_owner, _candidate_to_dict).
"""

from __future__ import annotations

import hmac
from pathlib import Path

from theater.constants.daemon import BUS_KIND_AGENT_TRANSCRIPT_RECEIPT, TRANSCRIPT_READABLE_KINDS
from theater.daemon.rpc.params import (
    _optional_string_param,
    _require,
    _string_param,
)
from theater.daemon.rpc.router import method
from theater.harness import HARNESSES, normalize
from theater.harness.source import TranscriptCandidate
from theater.harness.transcript.observer import (
    enumerate_transcript_candidates,
    open_participant_source,
)
from theater.models import (
    BadRequest,
    Status,
    Tier,
    TranscriptIdentityLost,
)
from theater.provenance import (
    TranscriptProvenance,
    is_trusted_provenance,
    normalize_provenance,
    provenance_at_least,
)
from theater.transcript_identity import (
    TRANSCRIPT_IDENTITY_LOST_CODE,
    canonical_location,
    same_location,
    transcript_identity_recovery_message,
)

CLAUDE_RECEIPT_RPC = "claude.receipt"
TRANSCRIPT_RECEIPT_RPC = "transcript.receipt"
TRANSCRIPT_RECEIPT_BUS_KIND = BUS_KIND_AGENT_TRANSCRIPT_RECEIPT

#: Kinds `read_transcript` reports. ERROR is dropped: not part of the conversation.
_READABLE = TRANSCRIPT_READABLE_KINDS


def _transcript_identity_lost(daemon, pid: str) -> bool:
    checker = getattr(daemon.observer, "transcript_identity_lost", None)
    return bool(checker(pid)) if callable(checker) else False


def _reject_cross_participant_receipt(
    daemon,
    *,
    participant_id: str,
    harness: str,
    session_id: str,
    transcript_location: str,
) -> None:
    canonical_harness = normalize(harness)
    for other in daemon.registry.list(include_dead=True):
        if other.id == participant_id:
            continue
        if normalize(other.harness) != canonical_harness:
            continue
        if same_location(other.transcript_location, transcript_location):
            raise BadRequest("transcript receipt location is already owned by another participant")
        if other.session_id == session_id and provenance_at_least(
            other.session_correlation, TranscriptProvenance.OPERATOR
        ):
            raise BadRequest(
                "transcript receipt session_id is already owned by another participant"
            )


def _reject_unbound_same_cwd_receipt(
    daemon,
    *,
    participant_id: str,
    harness: str,
    participant_session_id: str | None,
    participant_location: str | None,
    session_id: str,
    transcript_location: str,
) -> None:
    participant = daemon.store.get_participant(participant_id)
    if participant is None or not participant.cwd:
        return
    # The token proves this caller can read the hook's private file; not that the transcript is.
    cwd = Path(participant.cwd).resolve()
    canonical_harness = normalize(harness)
    for other in daemon.registry.list():
        if (
            other.id == participant_id
            or other.status is Status.DEAD
            or normalize(other.harness) != canonical_harness
            or not other.cwd
            or Path(other.cwd).resolve() != cwd
        ):
            continue
        if session_id == participant_session_id or same_location(
            participant_location, transcript_location
        ):
            return
        raise BadRequest(
            "transcript receipt cannot claim a new unbound transcript while "
            "another live participant of the same harness shares its cwd"
        )


def _candidate_owner(daemon, location: str, *, exclude: str | None = None):
    for other in daemon.registry.list(include_dead=True):
        if other.id != exclude and same_location(other.transcript_location, location):
            return other
    return None


def _candidate_to_dict(daemon, candidate) -> dict:
    location = canonical_location(candidate.location)
    owner = _candidate_owner(daemon, location)
    return {
        "location": location,
        "session_id": candidate.session_id,
        "mtime": candidate.mtime,
        "size": candidate.size,
        "provenance": candidate.provenance,
        "rejection_reason": candidate.rejection_reason,
        "domain": candidate.domain,
        "owner": owner.id if owner is not None and owner.status is not Status.DEAD else None,
        "tombstone": owner.id if owner is not None and owner.status is Status.DEAD else None,
    }


@method(TRANSCRIPT_RECEIPT_RPC)
async def _transcript_receipt(daemon, params: dict) -> dict:
    """Authenticated receipt of a harness's current transcript identity.

    Generic: core handles token auth, liveness, ownership-conflict policy,
    persistence, the bus audit event, watcher admission, and token renewal.
    The harness plugin's ``validate_transcript_receipt`` hook handles every
    format-specific concern (field names, path rules, record scans).
    """
    pid = _string_param(params, "id", method_name=TRANSCRIPT_RECEIPT_RPC)
    token = _string_param(params, "token", method_name=TRANSCRIPT_RECEIPT_RPC)
    raw_payload = params.get("payload")
    if not isinstance(raw_payload, dict):
        raise BadRequest(f"{TRANSCRIPT_RECEIPT_RPC} parameter 'payload' must be a JSON object")

    participant = daemon.store.get_participant(pid)
    if participant is None:
        raise BadRequest("transcript receipt id does not name an existing participant")
    if participant.status is Status.DEAD:
        daemon.store.delete_receipt_token(pid)
        raise BadRequest("transcript receipt id names a dead participant")
    expected = daemon.store.get_receipt_token(pid)
    if expected is None or not hmac.compare_digest(token, expected):
        raise BadRequest("transcript receipt token is invalid")

    # Resolve the observer through the daemon's harness registry, not the global HARNESSES.
    harness = daemon.observer.harnesses.get(normalize(participant.harness))
    observer = getattr(harness, "observer", None) if harness is not None else None
    if observer is None:
        raise BadRequest(
            f"transcript receipt: no observer registered for harness {participant.harness!r}"
        )

    # The plugin validates the opaque payload. Rejection is an exception (ValueError).
    try:
        candidate = observer.validate_transcript_receipt(
            payload=raw_payload,
            cwd=participant.cwd,
            expected_session_id=participant.session_id,
        )
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc

    # Core validates the returned candidate.
    if not isinstance(candidate, TranscriptCandidate):
        raise BadRequest(
            f"transcript receipt validator must return a TranscriptCandidate, "
            f"got {type(candidate).__name__}"
        )
    if not isinstance(candidate.location, str) or not candidate.location:
        raise BadRequest("transcript receipt validator returned an empty location")
    if not isinstance(candidate.session_id, str) or not candidate.session_id:
        raise BadRequest("transcript receipt validator returned an empty session_id")
    if candidate.rejection_reason:
        raise BadRequest(
            f"transcript receipt validator returned a rejection: {candidate.rejection_reason}"
        )

    location = candidate.location
    session_id = candidate.session_id
    _reject_cross_participant_receipt(
        daemon,
        participant_id=pid,
        harness=participant.harness,
        session_id=session_id,
        transcript_location=location,
    )
    _reject_unbound_same_cwd_receipt(
        daemon,
        participant_id=pid,
        harness=participant.harness,
        participant_session_id=participant.session_id,
        participant_location=participant.transcript_location,
        session_id=session_id,
        transcript_location=location,
    )
    admission = daemon.observer.transcript_receipt(pid, location=location, session_id=session_id)
    daemon.store.renew_receipt_token(pid)
    daemon.store.bus_append(
        TRANSCRIPT_RECEIPT_BUS_KIND,
        to_id=pid,
        payload={"location": location, "session_id": session_id, "admission": admission},
    )
    return {"ok": True, "admission": admission}


@method(CLAUDE_RECEIPT_RPC)
async def _claude_receipt_alias(daemon, params: dict) -> dict:
    """Backward-compatible alias: live Claude sessions invoke this name.

    Shipped in v3.2.0 with settings.json on disk referencing
    ``claude.receipt`` by that exact name. Forwards ``session_id`` and
    ``transcript_path`` into the generic ``transcript.receipt`` payload.
    """
    session_id = params.get("session_id")
    transcript_path = params.get("transcript_path")
    if session_id is not None and not isinstance(session_id, str):
        raise BadRequest("claude.receipt parameter 'session_id' must be a string")
    if transcript_path is not None and not isinstance(transcript_path, str):
        raise BadRequest("claude.receipt parameter 'transcript_path' must be a string")
    forwarded = dict(params)
    forwarded["payload"] = {
        k: v
        for k, v in (
            ("session_id", session_id),
            ("transcript_path", transcript_path),
        )
        if v is not None
    }
    return await _transcript_receipt(daemon, forwarded)


@method("transcript.candidates")
async def _transcript_candidates(daemon, params: dict) -> dict:
    p = daemon.registry.resolve(_require(params, "id"))
    harness_name = normalize(p.harness)
    harness = HARNESSES.get(harness_name)
    if harness is None:
        raise BadRequest(f"cannot enumerate candidates: harness {p.harness!r} is not known")
    after = p.created_at if p.tier is Tier.SPAWNED else None
    rows = enumerate_transcript_candidates(
        harness.observer,
        cwd=p.cwd,
        domain=p.transcript_domain,
        after=after,
    )
    return {"id": p.id, "candidates": [_candidate_to_dict(daemon, row) for row in rows]}


@method("transcript.bind")
async def _transcript_bind(daemon, params: dict) -> dict:
    target = _string_param(params, "id", method_name="transcript.bind")
    p = daemon.registry.resolve(target)
    pid = p.id
    if params.get("confirm_id") != pid:
        raise BadRequest("transcript.bind requires confirm_id to equal the stable participant id")
    raw_candidate = _string_param(params, "candidate", method_name="transcript.bind")
    transfer_from = _optional_string_param(params, "transfer_from", method_name="transcript.bind")
    if transfer_from is not None and params.get("transfer_confirm_id") != transfer_from:
        raise BadRequest(
            "transcript.bind transfer requires transfer_confirm_id to equal transfer_from"
        )

    harness_name = normalize(p.harness)
    harness = HARNESSES.get(harness_name)
    if harness is None:
        raise BadRequest(f"cannot bind transcript: harness {p.harness!r} is not known")
    try:
        admitted = harness.observer.admit_operator_candidate(
            cwd=p.cwd,
            candidate=raw_candidate,
            domain=p.transcript_domain,
            after=p.created_at if p.tier is Tier.SPAWNED else None,
        )
    except ValueError as exc:
        raise BadRequest(f"cannot bind transcript: {exc}") from None
    location = canonical_location(admitted.location)
    owner = _candidate_owner(daemon, location, exclude=pid)
    prior_owner = None
    if owner is not None:
        prior_owner = owner.id
        if transfer_from is None:
            raise BadRequest(
                f"candidate is already owned by participant {owner.id}; "
                "pass --transfer-from with that exact stable id to move it"
            )
        if transfer_from != owner.id:
            raise BadRequest(
                f"transfer-from must name the current owner exactly ({owner.id}), "
                f"got {transfer_from!r}"
            )
    elif transfer_from is not None:
        raise BadRequest("transfer-from was provided but the candidate has no current owner")

    from theater.models import now

    p = daemon.registry.get(pid)
    p.transcript_location = location
    p.session_id = admitted.session_id
    p.session_correlation = str(TranscriptProvenance.OPERATOR)
    p.transcript_domain = admitted.domain
    p.last_activity = now()
    audit_payload = {
        "actor_surface": "cli",
        "target": pid,
        "path": location,
        "session_id": admitted.session_id,
        "prior_owner": prior_owner,
    }
    daemon.store.bind_operator_transcript(
        target=p,
        prior_owner=owner,
        audit_payload=audit_payload,
    )
    if prior_owner is not None:
        await daemon.observer.reset_for_operator_bind(prior_owner)
    await daemon.observer.reset_for_operator_bind(pid)
    daemon.observer.record_operator_binding(
        pid,
        location,
        admitted.session_id,
        prior_owner=prior_owner,
    )
    return {
        "id": pid,
        "location": location,
        "session_id": admitted.session_id,
        "prior_owner": prior_owner,
    }


@method("read_transcript")
async def _read_transcript(daemon, params: dict) -> dict:
    """Read a participant's session back, with the text unclipped.

    Goes through the observer's `Source`, not through `find_transcript`, so an
    adapter whose output is a database answers this as well as one that writes
    a file. The source opened here is short-lived and separate from the
    watcher's: reading history must not move the watcher's cursor.
    """
    p = daemon.registry.resolve(_require(params, "id"))
    pid = p.id
    last_n = int(params.get("last_n", 5))

    harness_name = normalize(p.harness)
    harness = HARNESSES.get(harness_name)
    if harness is None:
        raise BadRequest(f"cannot read transcript: harness {p.harness!r} is not known")
    if _transcript_identity_lost(daemon, pid):
        raise TranscriptIdentityLost(transcript_identity_recovery_message(pid))

    # Same birth-time floor as the watch path: applies to SPAWNED participants only.
    after = p.created_at if p.tier is Tier.SPAWNED else None
    source = open_participant_source(
        harness.observer,
        participant_id=p.id,
        cwd=p.cwd,
        session_id=p.session_id,
        after=after,
        session_provenance=normalize_provenance(p.session_correlation),
        known_location=p.transcript_location,
        transcript_domain=p.transcript_domain,
        pane_pid=p.live_pid,
    )
    try:
        history = await source.history(last_n=last_n)
    finally:
        await source.aclose()

    if history.error_code is not None:
        if history.error_code == TRANSCRIPT_IDENTITY_LOST_CODE:
            if p.status is Status.DEAD:
                raise BadRequest(
                    "cannot read transcript: trusted dead binding is retained for resume, "
                    "but its transcript is unavailable"
                )
            raise TranscriptIdentityLost(transcript_identity_recovery_message(pid, history.error))
        raise BadRequest(
            f"cannot read transcript: {history.error or history.error_code} ({history.error_code})"
        )
    if history.location is None:
        raise BadRequest("cannot read transcript: transcript no longer exists on disk")
    if not is_trusted_provenance(history.correlation):
        raise BadRequest(
            "cannot read transcript: this session is known only from cwd/time; "
            "wait for exact/proven correlation or bind the session before reading it "
            "(transcript_correlation_untrusted)"
        )
    if daemon.observer.history_is_ambiguous(pid, history):
        raise BadRequest(
            "cannot read transcript: its session is known only from cwd/time and another "
            "live participant of the same harness shares that transcript root and cwd "
            "(transcript_correlation_ambiguous)"
        )
    events = [
        {
            "index": event.raw_index,
            "role": str(event.kind),
            "text": event.text or "",
            "tool_name": event.tool_name,
            "turn_end": event.turn_end,
        }
        for event in history.events
        if event.kind.value in _READABLE
    ]
    return {"id": pid, "events": events, "path": history.location}
