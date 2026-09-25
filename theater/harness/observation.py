"""Compatibility façade re-exporting the former observation definitions.

Names are the canonical objects; monkeypatches must target the module consumers import from,
since this façade does not forward attribute writes.
"""

from __future__ import annotations

from theater.harness.contracts.context import ParticipantObservationContext
from theater.harness.contracts.observation import (
    HarnessObserver,
    ScreenConfidence,
    ScreenKind,
    ScreenReading,
)
from theater.harness.transcript.observer import (
    TranscriptObserver,
    enumerate_transcript_candidates,
    open_participant_source,
)

__all__ = [
    "HarnessObserver",
    "ParticipantObservationContext",
    "ScreenConfidence",
    "ScreenKind",
    "ScreenReading",
    "TranscriptObserver",
    "enumerate_transcript_candidates",
    "open_participant_source",
]
