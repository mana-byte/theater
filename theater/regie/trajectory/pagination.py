"""Pure pagination for the trajectory ledger."""

from __future__ import annotations

from dataclasses import dataclass

from theater.regie.trajectory.search import (
    SearchResult,
    _base_request_entries,
    _compose_request_headers,
)


@dataclass(frozen=True, slots=True)
class LedgerPage:
    index: int
    count: int
    page_size: int
    total_items: int
    first_item: int
    last_item: int
    result: SearchResult

    @property
    def number(self) -> int:
        return self.index + 1

    @property
    def record_ids(self) -> tuple[str, ...]:
        return self.result.record_ids


def paginate_search_result(result: SearchResult, page: int, page_size: int) -> LedgerPage:
    if isinstance(page, bool) or not isinstance(page, int):
        raise TypeError("page must be an integer")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size < 1:
        raise ValueError("page size must be a positive integer")
    total = len(result.records)
    count = max(1, (total + page_size - 1) // page_size)
    index = max(0, min(page, count - 1))
    start = index * page_size
    end = min(total, start + page_size)
    records = result.records[start:end]
    record_ids = frozenset(record.record_id for record in records)
    group_ids = {
        group_id for record_id in record_ids for group_id in result.path_for_record(record_id)
    }
    all_requests = tuple(result.requests.values())
    all_by_record_id = {
        record_id: request.request_id
        for request in all_requests
        for record_id in request.record_ids
    }
    ordered_requests = tuple(
        request
        for request in all_requests
        if any(record_id in record_ids for record_id in request.record_ids)
    )
    by_record_id = {
        record_id: request.request_id
        for request in ordered_requests
        for record_id in request.record_ids
    }
    base_entries = _base_request_entries(
        result.entries,
        result.matched_ids,
        result.group_paths,
        all_by_record_id,
    )
    page_entries = tuple(
        entry
        for entry in base_entries
        if (
            entry.record_id in record_ids
            if entry.record_id is not None
            else entry.group_id in group_ids
        )
    )
    entries, requests = _compose_request_headers(
        page_entries,
        record_ids,
        result.group_paths,
        ordered_requests,
        by_record_id,
    )
    page_result = SearchResult(
        records=records,
        entries=tuple(entries),
        matched_ids=record_ids,
        scores={
            record.record_id: result.scores[record.record_id]
            for record in records
            if record.record_id in result.scores
        },
        counts=result.counts,
        group_paths={
            record.record_id: result.path_for_record(record.record_id) for record in records
        },
        requests=requests,
    )
    return LedgerPage(
        index=index,
        count=count,
        page_size=page_size,
        total_items=total,
        first_item=start + 1 if total else 0,
        last_item=end,
        result=page_result,
    )


__all__ = ["LedgerPage", "paginate_search_result"]
