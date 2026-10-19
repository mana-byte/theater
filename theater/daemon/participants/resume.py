"""Point-in-time participant resume verdicts."""

from __future__ import annotations

from theater.harness import HARNESSES, normalize, supports_resume
from theater.models import BadRequest, Participant, Status
from theater.provenance import is_trusted_provenance


def resume_state(p: Participant, live_peers: list[Participant]) -> str:
    """Derive the point-in-time resume verdict for one participant without extra DB queries.

    Gate order as spawn_session: live, no_session_id, harness_cannot_resume, owned_by_live
    (``live_peers``), untrusted, harness_resume_rejected, else resumable.
    """
    if p.status is not Status.DEAD:
        return "live"
    if any(other.resumed_from_id == p.id for other in live_peers):
        return "owned_by_live"
    if not p.session_id:
        return "no_session_id"
    harness = HARNESSES.get(normalize(p.harness))
    if harness is None or not supports_resume(harness):
        return "harness_cannot_resume"
    for other in live_peers:
        if (
            normalize(other.harness) == normalize(p.harness)
            and other.session_id == p.session_id
            and is_trusted_provenance(other.session_correlation)
        ):
            return "owned_by_live"
    if not is_trusted_provenance(p.session_correlation):
        return "untrusted"
    try:
        harness.resume_preflight(predecessor=p)
    except BadRequest:
        return "harness_resume_rejected"
    return "resumable"
