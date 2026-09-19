"""Bounded public discovery for resuming dead, trusted sessions."""

from __future__ import annotations

from dataclasses import dataclass

from theater.frontend import FrontendClient
from theater.frontend import ResumeCandidate as PublicResumeCandidate

RESUME_PAGE_SIZE = 20


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
    spawn_prompt: str | None = None
    last_activity: float | None = None
    resume_state: str = "resumable"


@dataclass(frozen=True, slots=True)
class ResumeDiscovery:
    """One deliberately bounded public page of historical sessions."""

    candidates: tuple[ResumeCandidate, ...]
    more_available: bool


async def discover_resume_sessions(client: FrontendClient) -> ResumeDiscovery:
    """Read daemon-classified resume candidates in recent-activity order."""
    page = await client.participants.resume_candidates(limit=RESUME_PAGE_SIZE)
    return ResumeDiscovery(
        candidates=tuple(_candidate_for(participant) for participant in page.value.items),
        more_available=page.value.next_cursor is not None,
    )


def _candidate_for(participant: PublicResumeCandidate) -> ResumeCandidate:
    session_id = participant.identity.session_id
    reason = _reason(participant.resume_state)
    available = participant.resume_state == "resumable"
    if available and (not participant.cwd or not session_id):
        available = False
        reason = (
            "the original working directory is unavailable"
            if not participant.cwd
            else "no trusted resume session is available"
        )
    return ResumeCandidate(
        participant.participant_id,
        participant.harness,
        participant.cwd,
        session_id,
        available,
        None if available else reason,
        participant.name,
        participant.description,
        participant.spawn_prompt,
        participant.last_activity,
        participant.resume_state,
    )


def _reason(state: str) -> str:
    return {
        "live": "session is still running",
        "no_session_id": "no session id was recorded",
        "harness_cannot_resume": "this harness does not support resume",
        "untrusted": "transcript identity could not be verified",
        "owned_by_live": "another live session holds this session id",
        "harness_resume_rejected": "the harness rejected this session as unsafe to resume",
    }.get(state, state or "unknown reason")


__all__ = ["RESUME_PAGE_SIZE", "ResumeCandidate", "ResumeDiscovery", "discover_resume_sessions"]
