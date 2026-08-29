"""Textual-free search, ordering, and paging state for a trajectory view."""

from __future__ import annotations

from theater.regie.trajectory.enums import DiagnosticView
from theater.regie.trajectory.render.diagnostics import ordering_for_projection
from theater.regie.trajectory.render.ordering import TrajectoryOrdering, build_ordering
from theater.regie.trajectory.render.pagination import LedgerPage, paginate_search_result
from theater.regie.trajectory.search import FilterCounts, SearchCache, SearchResult, search_records
from theater.regie.trajectory.state import ParticipantTrajectoryState
from theater.trajectory import TrajectoryGroup, TrajectoryRecord


class TrajectoryViewProjection:
    """Derived data for one participant trajectory, refreshed by its view."""

    def __init__(self, state: ParticipantTrajectoryState, page_size: int) -> None:
        self.search_cache = SearchCache()
        self.search_key: tuple[object, ...] | None = None
        self.search_result = SearchResult((), (), frozenset(), {}, FilterCounts())
        self.ordered_records: tuple[TrajectoryRecord, ...] = ()
        self.ordered_indices: dict[str, int] = {}
        self.all_visible_ids: tuple[str, ...] = ()
        self.all_visible_indices: dict[str, int] = {}
        self.visible_ids: tuple[str, ...] = ()
        self.visible_positions: tuple[int, ...] = ()
        self.visible_id_set: frozenset[str] = frozenset()
        self.visible_indices: dict[str, int] = {}
        self.ledger_page = paginate_search_result(self.search_result, state.ledger_page, page_size)

    def refresh(
        self,
        state: ParticipantTrajectoryState,
        page_size: int,
        *,
        recompute: bool = True,
    ) -> tuple[TrajectoryRecord, ...]:
        if recompute or not self.ordered_records:
            records = tuple(state.display_records)
            if state.diagnostic_view is DiagnosticView.ALL:
                default_ids = state.diagnostic_index.projection_for(DiagnosticView.ALL).record_ids
                records = tuple(record for record in records if record.record_id in default_ids)
            ordering = build_ordering(records, state.groups)
            self._recompute_search(state, ordering.records, ordering)
        self._sync_page(state, page_size)
        return self.ordered_records

    def _make_search_key(self, state: ParticipantTrajectoryState) -> tuple[object, ...]:
        record_key = tuple((record.record_id, record.revision) for record in state.records.values())
        groups = tuple(self._group_signature(group) for group in state.groups)
        requests = tuple(
            (request.request_id, request.record_ids) for request in state.request_index.ordered
        )
        return (
            record_key,
            state.query,
            frozenset(state.lane_filters),
            frozenset(state.kind_filters),
            frozenset(state.status_filters),
            frozenset(state.source_filters),
            state.diagnostic_view,
            groups,
            requests,
        )

    @staticmethod
    def _group_signature(group: TrajectoryGroup) -> tuple[object, ...]:
        return (
            group.group_id,
            group.label,
            group.record_ids,
            group.turn_id,
            group.step_id,
            tuple(TrajectoryViewProjection._group_signature(child) for child in group.children),
        )

    def _recompute_search(
        self,
        state: ParticipantTrajectoryState,
        records: tuple[TrajectoryRecord, ...],
        ordering: TrajectoryOrdering,
    ) -> None:
        self.ordered_records = records
        self.ordered_indices = {
            record.record_id: index for index, record in enumerate(self.ordered_records)
        }
        key = self._make_search_key(state)
        if key == self.search_key:
            return
        self.search_key = key
        projection = state.diagnostic_index.projection_for(state.diagnostic_view)
        diagnostic_ordering = ordering_for_projection(records, projection)
        self.search_result = search_records(
            records,
            query=state.query,
            lane_filters=state.lane_filters,
            kind_filters=state.kind_filters,
            status_filters=state.status_filters,
            source_filters=state.source_filters,
            groups=state.groups,
            cache=self.search_cache,
            ordering=diagnostic_ordering or ordering,
            request_index=state.request_index,
            tool_index=state.tool_index,
            candidate_ids=projection.record_ids,
            show_request_headers=False,
        )
        self.all_visible_ids = self.search_result.row_ids
        self.all_visible_indices = {
            record_id: index for index, record_id in enumerate(self.all_visible_ids)
        }

    def _sync_page(self, state: ParticipantTrajectoryState, page_size: int) -> None:
        selected_anchor = self.logical_row_id(state.selected_id)
        selected_index = self.all_visible_indices.get(selected_anchor or "")
        if state.follow_tail and self.all_visible_ids:
            requested_page = (len(self.all_visible_ids) - 1) // page_size
        elif selected_index is not None:
            requested_page = selected_index // page_size
        else:
            requested_page = state.ledger_page
        self.ledger_page = paginate_search_result(self.search_result, requested_page, page_size)
        state.ledger_page = self.ledger_page.index
        self.visible_ids = self.ledger_page.result.row_ids
        self.visible_id_set = frozenset(self.visible_ids)
        self.visible_indices = {
            record_id: index for index, record_id in enumerate(self.visible_ids)
        }
        self.visible_positions = tuple(
            self.ordered_indices[record_id]
            for record_id in self.visible_ids
            if record_id in self.ordered_indices
        )
        selected_anchor = self.logical_row_id(state.selected_id)
        if state.follow_tail:
            state.select(self.visible_ids[-1] if self.visible_ids else None)
        elif selected_anchor not in self.visible_id_set:
            state.select(self.visible_ids[0] if self.visible_ids else None)

    def logical_row_id(self, record_id: str | None) -> str | None:
        row_id = self.search_result.row_id_for_record(record_id)
        if row_id is not None:
            return row_id
        return record_id if record_id in self.all_visible_indices else None

    def page_for(self, page_index: int, page_size: int) -> LedgerPage:
        return paginate_search_result(self.search_result, page_index, page_size)


__all__ = ["TrajectoryViewProjection"]
