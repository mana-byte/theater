"""Claude implementations of the manifest callback seams."""

from __future__ import annotations

from theater.harness.contracts.callbacks import (
    NativeChildrenContext,
    OperatorCandidateContext,
    ReceiptValidationContext,
    ScreenContext,
    TranscriptCandidatesContext,
)
from theater.harness.contracts.context import ParticipantObservationContext
from theater.harness.contracts.launch import NativeChild
from theater.harness.contracts.observation import ScreenReading
from theater.harness.contracts.source import Source, TranscriptCandidate

from .observer import ClaudeCodeObserver


def source_factory(context: ParticipantObservationContext, *, root=None) -> Source:
    return ClaudeCodeObserver(root=root).open_source_context(context)


def screen_classifier(context: ScreenContext) -> ScreenReading:
    return ClaudeCodeObserver().screen_reading(context.capture)


def transcript_candidates(
    context: TranscriptCandidatesContext, *, root=None
) -> list[TranscriptCandidate]:
    return ClaudeCodeObserver(root=root).transcript_candidates(
        cwd=context.cwd, domain=context.domain, after=context.after
    )


def receipt_validator(context: ReceiptValidationContext, *, root=None) -> TranscriptCandidate:
    return ClaudeCodeObserver(root=root).validate_transcript_receipt(
        payload=context.payload,
        cwd=context.cwd,
        expected_session_id=context.expected_session_id,
    )


def operator_candidate_admitter(
    context: OperatorCandidateContext, *, root=None
) -> TranscriptCandidate:
    return ClaudeCodeObserver(root=root).admit_operator_candidate(
        cwd=context.cwd,
        candidate=context.candidate,
        domain=context.domain,
        after=context.after,
    )


def native_children(context: NativeChildrenContext) -> list[NativeChild]:
    return ClaudeCodeObserver().native_children(context.transcript)
