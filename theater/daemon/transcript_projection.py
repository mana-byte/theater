"""Pure public projection of one participant's transcript identity."""

from __future__ import annotations

from theater.harness.contracts.source import History
from theater.models import Participant
from theater.provenance import is_trusted_provenance


def participant_history(participant: Participant) -> History:
    """Build the identity value used by collision checks without doing I/O."""
    return History(
        location=participant.transcript_location,
        correlation=participant.session_correlation or "heuristic",
        collision_domain=participant.transcript_domain,
        pinned=participant.transcript_location is not None,
    )


def transcript_identity_projection(
    participant: Participant,
    *,
    lost: bool = False,
    ambiguous: bool = False,
    pending: bool = False,
) -> dict[str, object]:
    """Describe durable identity plus daemon-cached trust and collision facts."""
    state = "missing"
    detail: str | None = "no transcript identity has been observed"
    if lost:
        state = "lost"
        detail = "the previously trusted transcript identity was lost"
    elif ambiguous:
        state = "ambiguous"
        detail = "transcript ownership is ambiguous; inspect candidates and bind a verified session"
    elif pending:
        detail = "waiting for the first transcript; no transcript content is attributed yet"
    elif participant.session_id is not None or participant.transcript_location is not None:
        if not is_trusted_provenance(participant.session_correlation):
            state = "untrusted"
            detail = "the transcript identity has not been proven or bound by an operator"
        else:
            state = "trusted"
            detail = None
    return {
        "state": state,
        "session_id": participant.session_id,
        "provenance": participant.session_correlation,
        "location": participant.transcript_location,
        "domain": participant.transcript_domain,
        "detail": detail,
        **({"pending": True} if pending and state == "missing" else {}),
    }


def observed_transcript_identity(participant: Participant, observer) -> dict[str, object]:
    """Use the same cached observation facts in snapshots, participants, and control reports."""
    lost = _cached_flag(observer, "transcript_identity_lost", participant.id)
    ambiguous = _cached_flag(observer, "transcript_correlation_ambiguous", participant.id)
    if not lost and (participant.session_id is not None or participant.transcript_location):
        ambiguous = ambiguous or _cached_flag(
            observer, "history_is_ambiguous", participant.id, participant_history(participant)
        )
    return transcript_identity_projection(
        participant,
        lost=lost,
        ambiguous=ambiguous,
        pending=_cached_flag(observer, "transcript_pending", participant.id),
    )


def _cached_flag(observer, name: str, *args) -> bool:
    check = getattr(observer, name, None)
    try:
        return bool(callable(check) and check(*args))
    except Exception:
        return False


__all__ = ["observed_transcript_identity", "participant_history", "transcript_identity_projection"]
