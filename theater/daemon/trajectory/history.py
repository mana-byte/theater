"""Trusted, off-loop trajectory history loading through harness Sources."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass

from theater.daemon import workers
from theater.harness import normalize
from theater.harness.contracts.source import History, HistoryPage
from theater.harness.transcript.observer import open_participant_source
from theater.models import Participant, Status, Tier
from theater.provenance import is_trusted_provenance, normalize_provenance
from theater.transcript_identity import (
    TRANSCRIPT_IDENTITY_LOST_CODE,
    TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
    transcript_identity_recovery_message,
)


@dataclass(frozen=True, slots=True)
class HistoryLoad:
    page: HistoryPage
    source_epoch: str | None
    trusted: bool
    ambiguous: bool = False
    message: str = ""


async def load_history(
    daemon,
    participant: Participant,
    *,
    before: str | None = None,
    limit: int,
) -> HistoryLoad:
    """Open an independent source and run its bounded page read off-loop."""
    observer = _observer_for(daemon, participant)
    if observer is None or not getattr(observer, "has_transcript", True):
        return _unavailable("no transcript source is registered for this harness")
    if participant.status is not Status.DEAD and _identity_lost(daemon, participant.id):
        return _untrusted(transcript_identity_recovery_message(participant.id))
    after = participant.created_at if participant.tier is Tier.SPAWNED else None
    try:
        page = await workers.to_thread(
            _open_and_read_page,
            observer,
            participant,
            after,
            before,
            limit,
            label="trajectory.history_page",
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return _unavailable(f"trajectory history source failed: {exc}")
    if not isinstance(page, HistoryPage):
        return _unavailable(f"trajectory source returned {type(page).__name__}, not a HistoryPage")
    return _classify(daemon, participant, page)


def _open_and_read_page(
    observer,
    participant: Participant,
    after: float | None,
    before: str | None,
    limit: int,
) -> HistoryPage:
    source = open_participant_source(
        observer,
        participant_id=participant.id,
        cwd=participant.cwd,
        session_id=participant.session_id,
        after=after,
        session_provenance=normalize_provenance(participant.session_correlation),
        known_location=participant.transcript_location,
        transcript_domain=participant.transcript_domain,
        pane_pid=participant.live_pid,
    )

    async def read() -> HistoryPage:
        try:
            return await source.history_page(before=before, limit=limit)
        finally:
            await source.aclose()

    return asyncio.run(read())


def _classify(daemon, participant: Participant, page: HistoryPage) -> HistoryLoad:
    source_epoch = source_epoch_for(participant, page.location)
    if page.error_code is not None:
        if (
            page.error_code == TRANSCRIPT_IDENTITY_LOST_CODE
            and participant.status is not Status.DEAD
        ):
            return HistoryLoad(
                page=page,
                source_epoch=source_epoch,
                trusted=False,
                message=transcript_identity_recovery_message(participant.id, page.error),
            )
        if page.error_code == TRANSCRIPT_SOURCE_UNAVAILABLE_CODE:
            return HistoryLoad(
                page=page,
                source_epoch=source_epoch,
                trusted=False,
                message=page.error or "the transcript source is unavailable",
            )
        return HistoryLoad(
            page=page,
            source_epoch=source_epoch,
            trusted=False,
            message=page.error or f"the transcript source reported {page.error_code}",
        )
    if page.location is None and not page.events and not page.trajectory:
        return HistoryLoad(
            page=page,
            source_epoch=source_epoch,
            trusted=True,
            message="the transcript source is waiting for its first record",
        )
    history = History(
        location=page.location,
        events=page.events,
        correlation=page.provenance,
        pinned=page.pinned,
    )
    ambiguous = _history_ambiguous(daemon, participant.id, history)
    trusted = is_trusted_provenance(page.provenance) and not ambiguous
    if not trusted:
        message = (
            "this transcript is known only from cwd/time and is shared by another live "
            "participant; bind the session before viewing it"
            if ambiguous
            else "this transcript session is not proven to belong to the participant; "
            "wait for exact evidence or bind it before viewing"
        )
        return HistoryLoad(
            page=page,
            source_epoch=source_epoch,
            trusted=False,
            ambiguous=ambiguous,
            message=message,
        )
    return HistoryLoad(page=page, source_epoch=source_epoch, trusted=True)


def _observer_for(daemon, participant: Participant):
    observer_service = getattr(daemon, "observer", None)
    harnesses = getattr(observer_service, "harnesses", {})
    harness = harnesses.get(normalize(participant.harness))
    return getattr(harness, "observer", None) if harness is not None else None


def _history_ambiguous(daemon, pid: str, history: History) -> bool:
    checker = getattr(getattr(daemon, "observer", None), "history_is_ambiguous", None)
    if callable(checker):
        return bool(checker(pid, history))
    return False


def _identity_lost(daemon, pid: str) -> bool:
    checker = getattr(getattr(daemon, "observer", None), "transcript_identity_lost", None)
    return bool(checker(pid)) if callable(checker) else False


def source_epoch_for(participant: Participant, location: str | None) -> str:
    """Derive a stable per-participant source/session namespace."""
    identity = "|".join(
        (
            participant.id,
            normalize(participant.harness),
            participant.session_id or "",
            location or participant.transcript_location or "",
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _unavailable(message: str) -> HistoryLoad:
    page = HistoryPage(error_code=TRANSCRIPT_SOURCE_UNAVAILABLE_CODE, error=message)
    return HistoryLoad(page=page, source_epoch=None, trusted=False, message=message)


def _untrusted(message: str) -> HistoryLoad:
    page = HistoryPage(error_code=TRANSCRIPT_IDENTITY_LOST_CODE, error=message)
    return HistoryLoad(page=page, source_epoch=None, trusted=False, message=message)


__all__ = ["HistoryLoad", "load_history", "source_epoch_for"]
