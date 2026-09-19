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
) -> dict[str, object]:
    """Describe durable identity plus daemon-cached trust and collision facts."""
    state = "missing"
    detail: str | None = "no transcript identity has been observed"
    if lost:
        state = "lost"
        detail = "the previously trusted transcript identity was lost"
    elif participant.session_id is not None or participant.transcript_location is not None:
        if ambiguous:
            state = "ambiguous"
            detail = "the transcript identity collides with another live participant"
        elif not is_trusted_provenance(participant.session_correlation):
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
    }


__all__ = ["participant_history", "transcript_identity_projection"]
