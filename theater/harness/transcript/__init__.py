"""Transcript-file source implementation; the ``Source`` contract lives in ``contracts.source``."""

from __future__ import annotations

from theater.harness.contracts.trajectory import ParsedRecord, TrajectoryFact
from theater.harness.transcript.attachment import attach_point
from theater.harness.transcript.identity import file_stream_floor
from theater.harness.transcript.observer import (
    TranscriptObserver,
    enumerate_transcript_candidates,
    open_participant_source,
)
from theater.harness.transcript.source import TranscriptSource

__all__ = [
    "ParsedRecord",
    "TrajectoryFact",
    "TranscriptObserver",
    "TranscriptSource",
    "attach_point",
    "enumerate_transcript_candidates",
    "file_stream_floor",
    "open_participant_source",
]
