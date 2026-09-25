"""Textual-free timeline records and search matches for one trajectory view."""

from __future__ import annotations

from regie.trajectory.rich.render.ordering import build_ordering
from regie.trajectory.rich.render.records import has_content, is_raw_theater_bus_record
from regie.trajectory.rich.search import SearchCache, filter_matching_records
from regie.trajectory.rich.state import ParticipantTrajectoryState
from theater.frontend.trajectory import TrajectoryRecord


class TrajectoryViewProjection:
    """Chronological timeline records and the ids matching the current query."""

    def __init__(self) -> None:
        self._cache = SearchCache()
        self.all_records: tuple[TrajectoryRecord, ...] = ()
        self.all_indices: dict[str, int] = {}
        self.records: tuple[TrajectoryRecord, ...] = ()
        self.indices: dict[str, int] = {}
        self.matched_ids: frozenset[str] = frozenset()

    def refresh(self, state: ParticipantTrajectoryState) -> tuple[TrajectoryRecord, ...]:
        source = (
            state.remote_search_records if state.search_result_active else state.display_records
        )
        ordered = build_ordering(
            tuple(
                record
                for record in source
                if not is_raw_theater_bus_record(record) and has_content(record)
            ),
            state.groups,
        ).records
        # One span per tool operation: its members share one interval and would stack.
        self.all_records = tuple(
            record
            for record in ordered
            if state.row_anchor(record.record_id) in {record.record_id, None}
        )
        self.all_indices = {
            record.record_id: index for index, record in enumerate(self.all_records)
        }
        matching_records = filter_matching_records(ordered, state.query, self._cache)
        self.matched_ids = frozenset(
            anchor
            for record in matching_records
            if (anchor := state.row_anchor(record.record_id) or record.record_id)
            in self.all_indices
        )
        self.records = (
            tuple(record for record in self.all_records if record.record_id in self.matched_ids)
            if state.filter_matches and state.query.strip()
            else self.all_records
        )
        self.indices = {record.record_id: index for index, record in enumerate(self.records)}
        return self.records

    def nearest(self, record_id: str | None) -> str | None:
        """The visible span nearest to a prior selection in the full timeline."""
        if not self.records:
            return None
        origin = self.all_indices.get(record_id or "", len(self.all_records) - 1)
        return min(
            self.records,
            key=lambda record: (
                abs(self.all_indices[record.record_id] - origin),
                -self.all_indices[record.record_id],
            ),
        ).record_id

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
