"""Textual-free timeline records and search matches for one trajectory view."""

from __future__ import annotations

from regie.trajectory.domain import TrajectoryRecord
from regie.trajectory.rich.render.ordering import build_ordering
from regie.trajectory.rich.render.records import is_raw_theater_bus_record
from regie.trajectory.rich.search import SearchCache, matching_ids
from regie.trajectory.rich.state import ParticipantTrajectoryState


class TrajectoryViewProjection:
    """Chronological timeline records and the ids matching the current query."""

    def __init__(self) -> None:
        self._cache = SearchCache()
        self.records: tuple[TrajectoryRecord, ...] = ()
        self.indices: dict[str, int] = {}
        self.matched_ids: frozenset[str] = frozenset()

    def refresh(self, state: ParticipantTrajectoryState) -> tuple[TrajectoryRecord, ...]:
        source = (
            state.remote_search_records if state.search_result_active else state.display_records
        )
        ordered = build_ordering(
            tuple(record for record in source if not is_raw_theater_bus_record(record)),
            state.groups,
        ).records
        # One span per tool operation: its members share one interval and would stack.
        self.records = tuple(
            record
            for record in ordered
            if state.row_anchor(record.record_id) in {record.record_id, None}
        )
        self.indices = {record.record_id: index for index, record in enumerate(self.records)}
        self.matched_ids = frozenset(
            anchor
            for record_id in matching_ids(ordered, state.query, self._cache)
            if (anchor := state.row_anchor(record_id) or record_id) in self.indices
        )
        return self.records

    def match(self, record_id: str | None, delta: int) -> str | None:
        """The next (or previous) query match after the given record, wrapping around."""
        if not self.records or not self.matched_ids:
            return None
        start = self.indices.get(record_id or "", -1 if delta > 0 else len(self.records))
        for step in range(1, len(self.records) + 1):
            record = self.records[(start + step * delta) % len(self.records)]
            if record.record_id in self.matched_ids:
                return record.record_id
        return None


__all__ = ["TrajectoryViewProjection"]
