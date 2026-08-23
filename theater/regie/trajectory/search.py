"""Chronology-preserving fuzzy search and nested group filtering."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence, Set
from dataclasses import dataclass, field
from typing import TypeVar

from theater.regie.trajectory.constants import MAX_SEARCH_CACHE_ENTRIES
from theater.regie.trajectory.ordering import TrajectoryOrdering, build_ordering
from theater.trajectory import (
    GroupKind,
    TrajectoryGroup,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryStatus,
)

CacheKey = TypeVar("CacheKey")
CacheValue = TypeVar("CacheValue")


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
    values.extend(field.preview.text for field in record.details)
    values.extend(link.participant_id for link in record.links)
    values.extend(link.relation for link in record.links)
    return " ".join(values)


@dataclass(frozen=True, slots=True)
class TrajectoryFilters:
    """Independent filter sets used by one participant's view."""

    lanes: frozenset[TrajectoryLane] = frozenset()
    kinds: frozenset[TrajectoryKind] = frozenset()
    statuses: frozenset[TrajectoryStatus] = frozenset()
    sources: frozenset[str] = frozenset()

    @classmethod
    def from_sets(
        cls,
        *,
        lanes: Iterable[TrajectoryLane] = (),
        kinds: Iterable[TrajectoryKind] = (),
        statuses: Iterable[TrajectoryStatus] = (),
        sources: Iterable[str] = (),
    ) -> TrajectoryFilters:
        return cls(frozenset(lanes), frozenset(kinds), frozenset(statuses), frozenset(sources))


@dataclass(frozen=True, slots=True)
class FilterCounts:
    """Counts for each filter dimension while the other filters remain applied."""

    lanes: Mapping[TrajectoryLane, int] = field(default_factory=dict)
    kinds: Mapping[TrajectoryKind, int] = field(default_factory=dict)
    statuses: Mapping[TrajectoryStatus, int] = field(default_factory=dict)
    sources: Mapping[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class SearchCache:
    """Bounded searchable corpus and filter results keyed by record revision."""

    corpus: dict[tuple[str, int], str] = field(default_factory=dict)
    query_scores: dict[tuple[str, int, str], int | None] = field(default_factory=dict)
    filter_matches: dict[tuple[str, int, tuple[object, ...]], bool] = field(default_factory=dict)

    def _trim(self, cache: dict[CacheKey, CacheValue]) -> None:
        while len(cache) > MAX_SEARCH_CACHE_ENTRIES:
            del cache[next(iter(cache))]

    def searchable(self, record: TrajectoryRecord) -> str:
        key = (record.record_id, record.revision)
        text = self.corpus.get(key)
        if text is None:
            text = record_search_text(record).casefold()
            self.corpus[key] = text
            self._trim(self.corpus)
        return text

    def score(self, record: TrajectoryRecord, query: str) -> int | None:
        key = (record.record_id, record.revision, query)
        if key not in self.query_scores:
            self.query_scores[key] = fuzzy_subsequence_score(query, self.searchable(record))
            self._trim(self.query_scores)
        return self.query_scores[key]

    def passes(self, record: TrajectoryRecord, filters: TrajectoryFilters) -> bool:
        filter_key = (
            tuple(sorted(lane.value for lane in filters.lanes)),
            tuple(sorted(kind.value for kind in filters.kinds)),
            tuple(sorted(status.value for status in filters.statuses)),
            tuple(sorted(filters.sources)),
        )
        key = (record.record_id, record.revision, filter_key)
        if key not in self.filter_matches:
            self.filter_matches[key] = _passes_filters(record, filters)
            self._trim(self.filter_matches)
        return self.filter_matches[key]


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """A nested group header or an actual record row."""

    group_id: str
    group_label: str
    record_id: str | None = None
    collapsed: bool = False
    depth: int = 0
    group_kind: GroupKind | None = None

    @property
    def is_header(self) -> bool:
        return self.record_id is None


@dataclass(frozen=True, slots=True)
class SearchResult:
    """Filtered records plus structural headers retained for visible matches."""

    records: tuple[TrajectoryRecord, ...]
    entries: tuple[LedgerEntry, ...]
    matched_ids: frozenset[str]
    scores: Mapping[str, int]
    counts: FilterCounts
    group_paths: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def record_ids(self) -> tuple[str, ...]:
        return tuple(record.record_id for record in self.records)

    def path_for_record(self, record_id: str | None) -> tuple[str, ...]:
        return self.group_paths.get(record_id or "", ())


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
    lane_counts: Counter[TrajectoryLane] = Counter()
    kind_counts: Counter[TrajectoryKind] = Counter()
    status_counts: Counter[TrajectoryStatus] = Counter()
    source_counts: Counter[str] = Counter()
    for record in records:
        for name in ("lanes", "kinds", "statuses", "sources"):
            if not _passes_other_filters(record, filters, name):
                continue
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


def _matching_groups(groups: Sequence[TrajectoryGroup], matched_ids: frozenset[str]) -> set[int]:
    matches: set[int] = set()

    def visit(group: TrajectoryGroup) -> bool:
        found = any(record_id in matched_ids for record_id in group.record_ids)
        for child in group.children:
            found = visit(child) or found
        if found:
            matches.add(id(group))
        return found

    for group in groups:
        visit(group)
    return matches


def _group_paths(
    groups: Sequence[TrajectoryGroup],
) -> dict[str, tuple[str, ...]]:
    paths: dict[str, tuple[str, ...]] = {}

    def visit(group: TrajectoryGroup, parent: tuple[str, ...]) -> None:
        path = (*parent, group.group_id)
        for record_id in group.record_ids:
            paths.setdefault(record_id, path)
        for child in group.children:
            visit(child, path)

    for group in groups:
        visit(group, ())
    return paths


def search_records(
    records: Sequence[TrajectoryRecord],
    *,
    query: str = "",
    filters: TrajectoryFilters | None = None,
    lane_filters: Iterable[TrajectoryLane] = (),
    kind_filters: Iterable[TrajectoryKind] = (),
    status_filters: Iterable[TrajectoryStatus] = (),
    source_filters: Iterable[str] = (),
    groups: Sequence[TrajectoryGroup] = (),
    collapsed_groups: Set[str] = frozenset(),
    cache: SearchCache | None = None,
    ordering: TrajectoryOrdering | None = None,
) -> SearchResult:
    """Filter source order and retain every visible structural group header."""
    ordered = ordering or build_ordering(records, groups)
    complete = ordered.groups
    bounded_records = ordered.records
    active = filters or TrajectoryFilters.from_sets(
        lanes=lane_filters,
        kinds=kind_filters,
        statuses=status_filters,
        sources=source_filters,
    )
    normalized_query = query.casefold().strip()
    matched: list[TrajectoryRecord] = []
    scores: dict[str, int] = {}
    for record in bounded_records:
        passes = (
            cache.passes(record, active) if cache is not None else _passes_filters(record, active)
        )
        if not passes:
            continue
        score = (
            cache.score(record, normalized_query)
            if cache is not None
            else fuzzy_subsequence_score(normalized_query, record_search_text(record))
        )
        if score is None:
            continue
        matched.append(record)
        scores[record.record_id] = score

    matched_ids = frozenset(record.record_id for record in matched)
    paths = _group_paths(complete)
    matching_groups = _matching_groups(complete, matched_ids)
    entries: list[LedgerEntry] = []

    def visit(group: TrajectoryGroup, depth: int) -> None:
        if id(group) not in matching_groups:
            return
        collapsed = group.group_id in collapsed_groups
        entries.append(
            LedgerEntry(
                group_id=group.group_id,
                group_label=group.label,
                collapsed=collapsed,
                depth=depth,
                group_kind=group.kind,
            )
        )
        if collapsed:
            return
        for unit in ordered.group_units(group):
            if isinstance(unit, str) and unit in matched_ids:
                entries.append(
                    LedgerEntry(
                        group_id=group.group_id,
                        group_label=group.label,
                        record_id=unit,
                        depth=depth + 1,
                        group_kind=group.kind,
                    )
                )
            elif isinstance(unit, TrajectoryGroup) and id(unit) in matching_groups:
                visit(unit, depth + 1)

    for group in complete:
        visit(group, 0)
    return SearchResult(
        records=tuple(matched),
        entries=tuple(entries),
        matched_ids=matched_ids,
        scores=scores,
        counts=_filter_counts(bounded_records, active),
        group_paths=paths,
    )


def matches_query(record: TrajectoryRecord, query: str) -> bool:
    """Test one bounded record without changing chronology or filters."""
    return fuzzy_subsequence_score(query, record_search_text(record)) is not None


__all__ = [
    "FilterCounts",
    "LedgerEntry",
    "SearchCache",
    "SearchResult",
    "TrajectoryFilters",
    "fuzzy_subsequence_score",
    "matches_query",
    "record_search_text",
    "search_records",
]
