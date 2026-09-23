"""Query matching over loaded timeline records."""

from __future__ import annotations

from collections.abc import Sequence

from regie.trajectory.domain import TrajectoryRecord, record_search_score
from regie.trajectory.ui_constants import MAX_SEARCH_CACHE_ENTRIES


class SearchCache:
    """Scores keyed by record revision and query, bounded by insertion order."""

    def __init__(self) -> None:
        self._scores: dict[tuple[str, int, str], int | None] = {}

    def matches(self, record: TrajectoryRecord, query: str) -> bool:
        key = (record.record_id, record.revision, query)
        if key not in self._scores:
            self._scores[key] = record_search_score(record, query)
            while len(self._scores) > MAX_SEARCH_CACHE_ENTRIES:
                del self._scores[next(iter(self._scores))]
        return self._scores[key] is not None


def matching_ids(
    records: Sequence[TrajectoryRecord], query: str, cache: SearchCache | None = None
) -> frozenset[str]:
    """Every record when the query is blank, else the records it matches."""
    query = query.strip()
    if not query:
        return frozenset(record.record_id for record in records)
    cache = cache or SearchCache()
    return frozenset(record.record_id for record in records if cache.matches(record, query))


__all__ = ["SearchCache", "matching_ids"]
