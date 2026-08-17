"""Transcript identity provenance and trust predicates.

The database stores provenance as text for compatibility; this module is the
single place that gives those strings meaning.
"""

from __future__ import annotations

from enum import StrEnum


class TranscriptProvenance(StrEnum):
    HEURISTIC = "heuristic"
    OPERATOR = "operator"
    PROVEN = "proven"
    EXACT = "exact"


_RANK = {
    TranscriptProvenance.HEURISTIC: 0,
    TranscriptProvenance.OPERATOR: 1,
    TranscriptProvenance.PROVEN: 2,
    TranscriptProvenance.EXACT: 3,
}


def normalize_provenance(value: str | TranscriptProvenance | None) -> TranscriptProvenance:
    """Return a known provenance value, defaulting unknown legacy text to heuristic."""
    if isinstance(value, TranscriptProvenance):
        return value
    try:
        return TranscriptProvenance(value or TranscriptProvenance.HEURISTIC)
    except ValueError:
        return TranscriptProvenance.HEURISTIC


def provenance_at_least(
    value: str | TranscriptProvenance | None,
    minimum: str | TranscriptProvenance,
) -> bool:
    return _RANK[normalize_provenance(value)] >= _RANK[normalize_provenance(minimum)]


def is_trusted_provenance(value: str | TranscriptProvenance | None) -> bool:
    """Whether content may be attributed to a participant."""
    return provenance_at_least(value, TranscriptProvenance.OPERATOR)
