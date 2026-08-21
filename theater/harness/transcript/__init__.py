"""Transcript-file source implementation: tailing an append-only JSONL file.

The ``TranscriptSource`` and ``attach_point`` helper live here. The public
``Source`` contract and the data types it works with live in
``theater.harness.contracts.source``.
"""

from __future__ import annotations

from theater.harness.transcript.attachment import attach_point
from theater.harness.transcript.observer import (
    TranscriptObserver,
    enumerate_transcript_candidates,
    open_participant_source,
)
from theater.harness.transcript.source import TranscriptSource

__all__ = [
    "TranscriptObserver",
    "TranscriptSource",
    "attach_point",
    "enumerate_transcript_candidates",
    "open_participant_source",
]
