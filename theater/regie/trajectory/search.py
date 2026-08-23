"""Chronology-preserving fuzzy search and structural ledger filtering."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence, Set
from dataclasses import dataclass, field

from theater.regie.trajectory.models import (
    GroupMetadata,
    Lane,
    RecordKind,
    RecordStatus,
    TrajectoryRecord,
)


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
    values = [
        record.record_id,
        record.participant_id,
        record.source,
        record.summary,
        record.turn_id or "",
        record.step_id or "",
        record.call_id or "",
        record.parent_call_id or "",
    ]
    values.extend(field.name for field in record.details)
    values.extend(field.value.text for field in record.details)
    values.extend(link.participant_id for link in record.links)
    values.extend(link.label or "" for link in record.links)
    return " ".join(values)


@dataclass(frozen=True, slots=True)
class TrajectoryFilters:
    """Independent filter sets used by one participant's view."""

    lanes: frozenset[Lane] = frozenset()
    kinds: frozenset[RecordKind] = frozenset()
    statuses: frozenset[RecordStatus] = frozenset()
    sources: frozenset[str] = frozenset()

    @classmethod
    def from_sets(
        cls,
        *,
        lanes: Iterable[Lane] = (),
        kinds: Iterable[RecordKind] = (),
        statuses: Iterable[RecordStatus] = (),
        sources: Iterable[str] = (),
    ) -> TrajectoryFilters:
        return cls(frozenset(lanes), frozenset(kinds), frozenset(statuses), frozenset(sources))


@dataclass(frozen=True, slots=True)
class FilterCounts:
    """Counts for each filter dimension, while other active filters remain applied."""

    lanes: Mapping[Lane, int] = field(default_factory=dict)
    kinds: Mapping[RecordKind, int] = field(default_factory=dict)
    statuses: Mapping[RecordStatus, int] = field(default_factory=dict)
    sources: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """A structural group header or an actual record row."""

    group_id: str
    group_label: str
    record_id: str | None = None
    collapsed: bool = False

    @property
    def is_header(self) -> bool:
        return self.record_id is None


@dataclass(frozen=True, slots=True)
class SearchResult:
    """Filtered records plus headers retained for honest hierarchy."""

    records: tuple[TrajectoryRecord, ...]
    entries: tuple[LedgerEntry, ...]
    matched_ids: frozenset[str]
    scores: Mapping[str, int]
    counts: FilterCounts

    @property
    def record_ids(self) -> tuple[str, ...]:
        return tuple(record.record_id for record in self.records)


def _group_labels(
    records: Sequence[TrajectoryRecord], groups: Sequence[GroupMetadata] = ()
) -> dict[str, str]:
    labels = {group.group_id: group.label for group in groups}
    for record in records:
        labels.setdefault(
            record.group_id,
            f"Turn {record.turn_id}" if record.turn_id else "Between turns",
        )
    return labels


def _passes_filters(record: TrajectoryRecord, filters: TrajectoryFilters) -> bool:
    return (
        (not filters.lanes or record.lane in filters.lanes)
        and (not filters.kinds or record.kind in filters.kinds)
        and (not filters.statuses or record.status in filters.statuses)
        and (not filters.sources or record.source in filters.sources)
    )


def _passes_other_filters(
    record: TrajectoryRecord,
    filters: TrajectoryFilters,
    ignored: str,
) -> bool:
    return (
        (ignored == "lanes" or not filters.lanes or record.lane in filters.lanes)
        and (ignored == "kinds" or not filters.kinds or record.kind in filters.kinds)
        and (ignored == "statuses" or not filters.statuses or record.status in filters.statuses)
        and (ignored == "sources" or not filters.sources or record.source in filters.sources)
    )


def _filter_counts(records: Sequence[TrajectoryRecord], filters: TrajectoryFilters) -> FilterCounts:
    lane_counts: Counter[Lane] = Counter()
    kind_counts: Counter[RecordKind] = Counter()
    status_counts: Counter[RecordStatus] = Counter()
    source_counts: Counter[str] = Counter()
    for record in records:
        for name in ("lanes", "kinds", "statuses", "sources"):
            if _passes_other_filters(record, filters, name):
                if name == "lanes":
                    lane_counts[record.lane] += 1
                elif name == "kinds":
                    kind_counts[record.kind] += 1
                elif name == "statuses":
                    status_counts[record.status] += 1
                else:
                    source_counts[record.source] += 1
    return FilterCounts(
        lanes=dict(lane_counts),
        kinds=dict(kind_counts),
        statuses=dict(status_counts),
        sources=dict(source_counts),
    )


def search_records(
    records: Sequence[TrajectoryRecord],
    *,
    query: str = "",
    filters: TrajectoryFilters | None = None,
    lane_filters: Iterable[Lane] = (),
    kind_filters: Iterable[RecordKind] = (),
    status_filters: Iterable[RecordStatus] = (),
    source_filters: Iterable[str] = (),
    groups: Sequence[GroupMetadata] = (),
    collapsed_groups: Set[str] = frozenset(),
) -> SearchResult:
    """Filter in source order and retain group headers for every visible match."""
    active = filters or TrajectoryFilters.from_sets(
        lanes=lane_filters,
        kinds=kind_filters,
        statuses=status_filters,
        sources=source_filters,
    )
    normalized_query = query.casefold().strip()
    matched: list[TrajectoryRecord] = []
    scores: dict[str, int] = {}
    for record in records:
        if not _passes_filters(record, active):
            continue
        score = fuzzy_subsequence_score(normalized_query, record_search_text(record))
        if score is None:
            continue
        matched.append(record)
        scores[record.record_id] = score

    labels = _group_labels(records, groups)
    entries: list[LedgerEntry] = []
    last_group: str | None = None
    visible_ids = frozenset(record.record_id for record in matched)
    for record in matched:
        group_id = record.group_id
        if group_id != last_group:
            entries.append(
                LedgerEntry(
                    group_id=group_id,
                    group_label=labels[group_id],
                    collapsed=group_id in collapsed_groups,
                )
            )
            last_group = group_id
        if group_id not in collapsed_groups:
            entries.append(
                LedgerEntry(
                    group_id=group_id,
                    group_label=labels[group_id],
                    record_id=record.record_id,
                )
            )
    return SearchResult(
        records=tuple(matched),
        entries=tuple(entries),
        matched_ids=visible_ids,
        scores=scores,
        counts=_filter_counts(records, active),
    )


def matches_query(record: TrajectoryRecord, query: str) -> bool:
    """Test one bounded record without changing chronology or filters."""
    return fuzzy_subsequence_score(query, record_search_text(record)) is not None


__all__ = [
    "FilterCounts",
    "LedgerEntry",
    "SearchResult",
    "TrajectoryFilters",
    "fuzzy_subsequence_score",
    "matches_query",
    "record_search_text",
    "search_records",
]
