"""Compatibility façade re-exporting the former source-contract and transcript symbols.

The canonical Source contract (ABC and data types) now lives in
``theater.harness.contracts.source``. The transcript-file implementation
(``TranscriptSource`` and ``attach_point``) lives in
``theater.harness.transcript``. This module preserves every former defined
name, signature, and object identity so existing imports continue to work
unchanged.
"""

from __future__ import annotations

import logging

from theater.harness.contracts.source import (
    Attachment,
    Batch,
    History,
    HistoryPage,
    IdentityLossEvidence,
    ReceiptAdmission,
    Source,
    SourceContractError,
    StreamPoint,
    TrajectoryHistoryPage,
    TranscriptCandidate,
)
from theater.harness.contracts.trajectory import FactLink, ParsedRecord, TrajectoryFact
from theater.harness.transcript.attachment import attach_point
from theater.harness.transcript.source import TranscriptSource

logger = logging.getLogger("theater.harness.source")

__all__ = [
    "Attachment",
    "Batch",
    "FactLink",
    "History",
    "HistoryPage",
    "IdentityLossEvidence",
    "ParsedRecord",
    "ReceiptAdmission",
    "Source",
    "SourceContractError",
    "StreamPoint",
    "TrajectoryFact",
    "TrajectoryHistoryPage",
    "TranscriptCandidate",
    "TranscriptSource",
    "attach_point",
]
