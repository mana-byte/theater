"""Shared transcript attribution checks for send admission and projection."""

from __future__ import annotations

from collections.abc import Callable
from typing import NoReturn

from theater.harness import HARNESSES, normalize
from theater.models import TheaterError, Tier, TranscriptIdentityLost, TranscriptUntrusted
from theater.provenance import is_trusted_provenance
from theater.transcript_identity import (
    TRANSCRIPT_IDENTITY_LOST_CODE,
    transcript_identity_recovery_message,
)


def _transcript_identity_lost(daemon, pid: str) -> bool:
    checker = getattr(daemon.observer, "transcript_identity_lost", None)
    return bool(checker(pid)) if callable(checker) else False


def transcript_send_failure(daemon, target) -> tuple[TheaterError, str] | None:
    """Describe a transcript attribution refusal without recording it."""
    if _transcript_identity_lost(daemon, target.id):
        return (
            TranscriptIdentityLost(transcript_identity_recovery_message(target.id)),
            TRANSCRIPT_IDENTITY_LOST_CODE,
        )
    if is_trusted_provenance(target.session_correlation):
        return None
    ambiguity = getattr(daemon.observer, "transcript_correlation_ambiguous", None)
    if target.tier is not Tier.ADOPTED and not (callable(ambiguity) and ambiguity(target.id)):
        return None
    harness = HARNESSES.get(normalize(target.harness))
    if harness is None or not harness.observer.has_transcript:
        return None
    pid = target.id
    return (
        TranscriptUntrusted(
            f"participant {pid!r} has no trusted transcript identity: attribution is not yet "
            "operator/proven/exact. Screen-only status observation remains live, but "
            "Theater will not create a send job until attribution is trusted. Run "
            f"`theater candidates {pid}` to inspect candidates, then "
            f"`theater bind {pid} <candidate> --confirm-id {pid}` for the candidate "
            "you verified. If no candidates are listed yet, retry after the next "
            "observation poll before binding."
        ),
        "transcript_untrusted",
    )


def check_transcript_send_preflight(daemon, target, refuse: Callable[..., NoReturn]) -> None:
    """Refuse untrusted adoption, observed ambiguity, or identity loss before job creation."""
    failure = transcript_send_failure(daemon, target)
    if failure is not None:
        exc, reason = failure
        refuse(exc, reason=reason)
