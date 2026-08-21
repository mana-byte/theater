"""Compatibility façade re-exporting the former observation definitions.

The canonical contract types (``ScreenKind``, ``ScreenConfidence``,
``ScreenReading``, ``HarnessObserver``) now live in
``theater.harness.contracts.observation``. The transcript-specific mechanics
(``enumerate_transcript_candidates``, ``open_participant_source``,
``TranscriptObserver``) live in ``theater.harness.transcript.observer``.
This module preserves every former defined name, signature, decorator, and
object identity: ``from theater.harness.observation import X`` yields the
very object defined in the canonical module. Assignment-based monkeypatches
(e.g. ``theater.harness.observation.SomeClass = stub``) must target the
module the consumer actually imports from, because this façade does not
forward attribute writes.
"""

from __future__ import annotations

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
    "ScreenConfidence",
    "ScreenKind",
    "ScreenReading",
    "TranscriptObserver",
    "enumerate_transcript_candidates",
    "open_participant_source",
]
