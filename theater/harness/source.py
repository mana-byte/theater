"""Compatibility façade re-exporting the former source-contract and transcript symbols."""

from __future__ import annotations

import logging

from theater.harness.contracts.source import (
    BATCH_TERMINAL_EVIDENCE_MAX,
    Attachment,
    Batch,
    History,
    IdentityLossEvidence,
    ReceiptAdmission,
    Source,
    SourceContractError,
    StreamPoint,
    TranscriptCandidate,
)
from theater.harness.transcript.attachment import attach_point
from theater.harness.transcript.source import TranscriptSource

logger = logging.getLogger("theater.harness.source")

__all__ = [
    "BATCH_TERMINAL_EVIDENCE_MAX",
    "Attachment",
    "Batch",
    "History",
    "IdentityLossEvidence",
    "ReceiptAdmission",
    "Source",
    "SourceContractError",
    "StreamPoint",
    "TranscriptCandidate",
    "TranscriptSource",
    "attach_point",
]
