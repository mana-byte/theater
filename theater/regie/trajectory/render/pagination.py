"""Pure pagination for the trajectory ledger."""

from __future__ import annotations

from dataclasses import dataclass

from theater.regie.trajectory.search import (
    SearchResult,
    base_request_entries,
    compose_request_headers,
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
        return self.result.row_ids


def paginate_search_result(result: SearchResult, page: int, page_size: int) -> LedgerPage:
    if isinstance(page, bool) or not isinstance(page, int):
        raise TypeError("page must be an integer")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size < 1:
        raise ValueError("page size must be a positive integer")
    row_ids = result.row_ids
    total = len(row_ids)
    count = max(1, (total + page_size - 1) // page_size)
    index = max(0, min(page, count - 1))
    start = index * page_size
    end = min(total, start + page_size)
    page_row_ids = frozenset(row_ids[start:end])
    records = tuple(
        record
        for record in result.records
        if result.row_id_for_record(record.record_id) in page_row_ids
    )
    record_ids = frozenset(record.record_id for record in records)
    group_ids = {group_id for row_id in page_row_ids for group_id in result.path_for_record(row_id)}
    all_requests = tuple(result.requests.values())
    all_by_record_id = dict(result.request_id_by_row_id)
    mapped_request_ids = frozenset(all_by_record_id.values())
    ordered_requests = tuple(
        request for request in all_requests if request.request_id in mapped_request_ids
    )
    known_request_ids = frozenset(request.request_id for request in ordered_requests)
    by_record_id = {
        record_id: request_id
        for record_id, request_id in all_by_record_id.items()
        if request_id in known_request_ids
    }
    base_entries = (
        base_request_entries(
            result.entries,
            page_row_ids,
            result.group_paths,
            all_by_record_id,
        )
        if result.show_request_headers
        else result.entries
    )
    page_entries = tuple(
        entry
        for entry in base_entries
        if (
            entry.record_id in page_row_ids
            if entry.record_id is not None
            else entry.group_id in group_ids
        )
    )
    if result.show_request_headers:
        entries, requests = compose_request_headers(
            page_entries,
            page_row_ids,
            result.group_paths,
            ordered_requests,
            by_record_id,
        )
    else:
        entries = list(page_entries)
        requests = {request.request_id: request for request in ordered_requests}
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
        group_paths={row_id: result.path_for_record(row_id) for row_id in page_row_ids},
        requests=requests,
        tools={
            entry.tool_operation_id: result.tools[entry.tool_operation_id]
            for entry in entries
            if entry.tool_operation_id is not None and entry.tool_operation_id in result.tools
        },
        row_id_by_record_id={
            record_id: row_id
            for record_id, row_id in result.row_id_by_record_id.items()
            if row_id in page_row_ids
        },
        request_id_by_row_id={
            row_id: request_id
            for row_id, request_id in result.request_id_by_row_id.items()
            if row_id in page_row_ids
        },
        show_request_headers=result.show_request_headers,
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
