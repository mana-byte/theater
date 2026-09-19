"""Bounded public discovery for resuming dead, trusted sessions."""

from __future__ import annotations

from dataclasses import dataclass

from theater.frontend import FrontendClient, Participant

RESUME_PAGE_SIZE = 50


@dataclass(frozen=True, slots=True)
class ResumeCandidate:
    """One visible historical session, whether or not it can be resumed now."""

    participant_id: str
    harness: str
    cwd: str | None
    session_id: str | None
    available: bool
    reason: str | None = None
    name: str | None = None
    description: str | None = None


@dataclass(frozen=True, slots=True)
class ResumeDiscovery:
    """One deliberately bounded public page of historical sessions."""

    candidates: tuple[ResumeCandidate, ...]
    more_available: bool


async def discover_resume_sessions(client: FrontendClient) -> ResumeDiscovery:
    """Read one bounded dead-participant page without inferring private provenance."""
    page = await client.participants.list(status="dead", limit=RESUME_PAGE_SIZE)
    return ResumeDiscovery(
        candidates=tuple(_candidate_for(participant) for participant in page.value.items),
        more_available=page.value.next_cursor is not None,
    )


def _candidate_for(participant: Participant) -> ResumeCandidate:
    if participant.status != "dead":
        return ResumeCandidate(
            participant.participant_id,
            participant.harness,
            participant.cwd,
            None,
            False,
            "participant is no longer dead",
            participant.name,
            participant.description,
        )
    identity = participant.trusted_identity
    session_id = identity.get("session_id") if identity is not None else None
    if not isinstance(session_id, str) or not session_id:
        return ResumeCandidate(
            participant.participant_id,
            participant.harness,
            participant.cwd,
            None,
            False,
            "no trusted resume session is available",
            participant.name,
            participant.description,
        )
    if participant.cwd is None or not participant.cwd:
        return ResumeCandidate(
            participant.participant_id,
            participant.harness,
            participant.cwd,
            session_id,
            False,
            "the original working directory is unavailable",
            participant.name,
            participant.description,
        )
    return ResumeCandidate(
        participant.participant_id,
        participant.harness,
        participant.cwd,
        session_id,
        True,
        None,
        participant.name,
        participant.description,
    )


__all__ = ["RESUME_PAGE_SIZE", "ResumeCandidate", "ResumeDiscovery", "discover_resume_sessions"]
