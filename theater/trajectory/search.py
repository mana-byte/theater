"""Process-neutral trajectory search text and field-aware relevance scoring."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Self

from theater.constants.trajectory import (
    TRAJECTORY_SEARCH_QUERY_MAX_BYTES,
    TRAJECTORY_SEARCH_RESULT_LIMIT,
)
from theater.trajectory.content import ContentPreview, bounded_text
from theater.trajectory.enums import TrajectoryValidationError
from theater.trajectory.records import TrajectoryRecord
from theater.trajectory.validation import boolean, integer, keys, mapping, sequence, string

_TOKEN = re.compile(r"[\w./:-]+", re.UNICODE)


def fuzzy_subsequence_score(query: str, candidate: str) -> int | None:
    """Return a stable match score, or None when query is not an ordered subsequence."""
    query = query.casefold().strip()
    candidate = candidate.casefold()
    if not query:
        return 0
    position = 0
    score = 0
    previous = -2
    for character in query:
        found = candidate.find(character, position)
        if found < 0:
            return None
        score += 1
        if found == previous + 1:
            score += 4
        if found == 0 or candidate[found - 1].isspace() or candidate[found - 1] in "-_/:.":
            score += 3
        score += max(0, 2 - found // 24)
        previous = found
        position = found + 1
    return score


def record_search_text(record: TrajectoryRecord) -> str:
    """Build searchable text only from already bounded record fields."""
    return " ".join(text for _weight, text in record_search_fields(record))


def record_search_fields(record: TrajectoryRecord) -> tuple[tuple[int, str], ...]:
    """Return weighted bounded fields, strongest structural identities first."""
    fields: list[tuple[int, str]] = []

    def add(weight: int, *values: object) -> None:
        fields.extend((weight, value) for value in values if isinstance(value, str) and value)

    add(100, record.mcp_server)
    add(90, record.mcp_tool)
    if record.failure is not None:
        add(80, record.failure.category.value, record.failure.code, record.failure.detail)
    add(70, record.summary)
    for detail in record.details:
        name = detail.name.casefold()
        if any(marker in name for marker in ("input", "argument", "parameter", "request")):
            weight = 50
        elif any(marker in name for marker in ("result", "output", "response")):
            weight = 35
        else:
            weight = 40
        add(45, detail.name)
        add(weight, detail.preview.text)
    if record.usage is not None:
        add(
            25,
            record.usage.provider,
            record.usage.model,
            record.usage.request_id,
            record.usage.cost_provenance.value,
        )
    add(20, record.source)
    for link in record.links:
        add(20, link.participant_id, link.relation)
    add(
        10,
        record.record_id,
        record.participant_id,
        record.turn_id,
        record.step_id,
        record.call_id,
        record.parent_call_id,
        record.request_id,
        record.retry_of_record_id,
        str(record.retry_attempt or ""),
    )
    return tuple(fields)


def _within_one_edit(left: str, right: str) -> bool:
    """Cheap bounded typo test: one insert, delete, substitute, or adjacent swap."""
    if left == right:
        return True
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        mismatches = [
            index for index, pair in enumerate(zip(left, right, strict=True)) if pair[0] != pair[1]
        ]
        if len(mismatches) <= 1:
            return True
        return (
            len(mismatches) == 2
            and mismatches[1] == mismatches[0] + 1
            and left[mismatches[0]] == right[mismatches[1]]
            and left[mismatches[1]] == right[mismatches[0]]
        )
    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    short_index = 0
    long_index = 0
    skipped = False
    while short_index < len(shorter) and long_index < len(longer):
        if shorter[short_index] == longer[long_index]:
            short_index += 1
            long_index += 1
            continue
        if skipped:
            return False
        skipped = True
        long_index += 1
    return True


def _field_quality(term: str, text: str) -> int | None:
    candidate = text.casefold()
    tokens = _TOKEN.findall(candidate)
    if term == candidate or term in tokens:
        return 100
    if any(token.startswith(term) for token in tokens):
        return 90
    if any(term in token for token in tokens) or term in candidate:
        return 80
    if len(term) >= 4 and any(_within_one_edit(term, token) for token in tokens):
        return 75
    if len(term) >= 3 and (fuzzy := fuzzy_subsequence_score(term, candidate)) is not None:
        return min(69, 50 + fuzzy // 5)
    return None


def record_search_score(record: TrajectoryRecord, query: str) -> int | None:
    """Score every query token against bounded fields; all tokens must match."""
    normalized = query.casefold().strip()
    if not normalized:
        return 0
    terms = tuple(_TOKEN.findall(normalized))
    if not terms:
        return None
    fields = record_search_fields(record)
    total = 0
    winning_fields: list[int] = []
    for term in terms:
        candidates: list[tuple[int, int]] = []
        for index, (weight, text) in enumerate(fields):
            quality = _field_quality(term, text)
            if quality is not None:
                candidates.append((weight * 100 + quality, index))
        if not candidates:
            return None
        score, field_index = max(candidates)
        total += score
        winning_fields.append(field_index)
    if len(terms) > 1 and len(set(winning_fields)) == 1:
        total += 25
    if any(normalized in text.casefold() for _weight, text in fields):
        total += 50
    return total


def ranked_records(
    records: Iterable[TrajectoryRecord], query: str
) -> tuple[tuple[TrajectoryRecord, int], ...]:
    """Return matching records by relevance, then newest native timing."""
    matches = []
    for record in records:
        score = record_search_score(record, query)
        if score is not None:
            timestamp = (
                record.timing.end
                if record.timing is not None and record.timing.end is not None
                else record.timing.start
                if record.timing is not None and record.timing.start is not None
                else float(record.raw_index)
            )
            matches.append((record, score, timestamp))
    matches.sort(key=lambda value: (value[1], value[2], value[0].record_id), reverse=True)
    return tuple((record, score) for record, score, _timestamp in matches)


@dataclass(frozen=True, slots=True)
class TrajectorySearchResult:
    """Bounded full-history search response."""

    query: str
    records: tuple[TrajectoryRecord, ...] = ()
    scanned_records: int = 0
    matched_records: int = 0
    complete: bool = True
    truncated: bool = False
    message: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "query",
            bounded_text(
                self.query,
                max_bytes=TRAJECTORY_SEARCH_QUERY_MAX_BYTES,
                label="search.query",
                nonempty=True,
            ),
        )
        object.__setattr__(self, "records", tuple(self.records))
        if len(self.records) > TRAJECTORY_SEARCH_RESULT_LIMIT:
            raise TrajectoryValidationError(
                f"search.records exceeds {TRAJECTORY_SEARCH_RESULT_LIMIT} values"
            )
        if any(not isinstance(record, TrajectoryRecord) for record in self.records):
            raise TrajectoryValidationError("search.records must contain TrajectoryRecord values")
        for name in ("scanned_records", "matched_records"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise TrajectoryValidationError(f"search.{name} must be a non-negative integer")
        if type(self.complete) is not bool or type(self.truncated) is not bool:
            raise TrajectoryValidationError("search boolean fields must be booleans")
        object.__setattr__(self, "message", ContentPreview.from_text(self.message).text)

    def to_wire(self) -> dict[str, object]:
        return {
            "query": self.query,
            "records": [record.to_wire() for record in self.records],
            "scanned_records": self.scanned_records,
            "matched_records": self.matched_records,
            "complete": self.complete,
            "truncated": self.truncated,
            "message": self.message,
        }

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = mapping(value, "trajectory search result")
        keys(
            data,
            required={"query"},
            optional={
                "records",
                "scanned_records",
                "matched_records",
                "complete",
                "truncated",
                "message",
            },
            label="trajectory search result",
        )
        return cls(
            query=string(data["query"], "search.query"),
            records=tuple(
                TrajectoryRecord.from_wire(item)
                for item in sequence(data.get("records", []), "search.records")
            ),
            scanned_records=integer(data.get("scanned_records", 0), "search.scanned_records"),
            matched_records=integer(data.get("matched_records", 0), "search.matched_records"),
            complete=boolean(data.get("complete", True), "search.complete"),
            truncated=boolean(data.get("truncated", False), "search.truncated"),
            message=string(data.get("message", ""), "search.message"),
        )


__all__ = [
    "TrajectorySearchResult",
    "fuzzy_subsequence_score",
    "ranked_records",
    "record_search_fields",
    "record_search_score",
    "record_search_text",
]
