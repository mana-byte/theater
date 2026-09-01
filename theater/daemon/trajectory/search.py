"""Independent full-history trajectory search."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from theater.constants.daemon import BUS_PARTICIPANT_PAGE_MAX_LIMIT
from theater.constants.trajectory import (
    TRAJECTORY_PAGE_RECORD_LIMIT,
    TRAJECTORY_RESPONSE_MAX_BYTES,
)
from theater.daemon.trajectory.bus_ingest import project_bus_rows
from theater.daemon.trajectory.history import load_history
from theater.daemon.trajectory.project import project_history_page
from theater.daemon.trajectory.responses import wire_bytes
from theater.daemon.trajectory.theater_events import ALLOWLISTED_BUS_KINDS
from theater.models import Participant
from theater.trajectory import (
    TrajectoryRecord,
    TrajectorySearchResult,
    deterministic_record_order,
    ranked_records,
    record_search_score,
)


class _SearchAccumulator:
    def __init__(self, query: str, limit: int) -> None:
        self.query = query
        self.limit = limit
        self.records: dict[str, tuple[TrajectoryRecord, int]] = {}
        self.scanned = 0
        self.matched = 0

    def add(self, records: tuple[TrajectoryRecord, ...]) -> None:
        self.scanned += len(records)
        for record in records:
            score = record_search_score(record, self.query)
            if score is None:
                continue
            self.matched += 1
            current = self.records.get(record.record_id)
            if current is None or record.revision > current[0].revision:
                self.records[record.record_id] = (record, score)
        if len(self.records) > self.limit * 2:
            self._prune(self.limit)

    def ranked(self) -> tuple[tuple[TrajectoryRecord, int], ...]:
        return ranked_records((value[0] for value in self.records.values()), self.query)

    def _prune(self, limit: int) -> None:
        self.records = {
            record.record_id: (record, score) for record, score in self.ranked()[:limit]
        }


async def search_history(
    daemon,
    participant: Participant,
    *,
    query: str,
    limit: int,
) -> TrajectorySearchResult:
    """Scan transcript and Theater bus without warming or mutating trajectory streams."""
    accumulator = _SearchAccumulator(query, limit)
    messages: list[str] = []
    transcript_complete = await _scan_transcript(
        daemon,
        participant,
        accumulator,
        messages,
    )
    bus_complete = await _scan_bus(daemon, participant, accumulator, messages)
    return _fit_result(
        query,
        accumulator,
        complete=transcript_complete and bus_complete,
        message="; ".join(messages),
    )


async def _scan_transcript(
    daemon,
    participant: Participant,
    accumulator: _SearchAccumulator,
    messages: list[str],
) -> bool:
    before: str | None = None
    seen_cursors: set[str] = set()
    while True:
        result = await load_history(
            daemon,
            participant,
            before=before,
            limit=TRAJECTORY_PAGE_RECORD_LIMIT,
        )
        if not result.trusted:
            messages.append(result.message or "transcript history is unavailable")
            return False
        source_epoch = result.source_epoch
        if source_epoch is None:
            messages.append("transcript history has no stable source identity")
            return False
        accumulator.add(
            project_history_page(
                result.page,
                participant_id=participant.id,
                source_epoch=source_epoch,
            )
        )
        if not result.page.has_older:
            return True
        older = result.page.older_cursor
        if older is None or older == before or older in seen_cursors:
            messages.append("transcript history returned a repeated older cursor")
            return False
        seen_cursors.add(older)
        before = older
        await asyncio.sleep(0)


async def _scan_bus(
    daemon,
    participant: Participant,
    accumulator: _SearchAccumulator,
    messages: list[str],
) -> bool:
    before_id: int | None = None
    while True:
        try:
            rows = daemon.store.bus_page_for_participant(
                participant.id,
                before_id=before_id,
                limit=BUS_PARTICIPANT_PAGE_MAX_LIMIT,
                kinds=ALLOWLISTED_BUS_KINDS,
            )
        except Exception as exc:
            messages.append(f"Theater bus history is unavailable: {exc}")
            return False
        accumulator.add(project_bus_rows(rows, participant.id))
        if len(rows) < BUS_PARTICIPANT_PAGE_MAX_LIMIT:
            return True
        oldest = rows[0].get("id")
        if type(oldest) is not int or oldest < 0 or oldest == before_id:
            messages.append("Theater bus history returned an invalid older cursor")
            return False
        before_id = oldest
        await asyncio.sleep(0)


def _fit_result(
    query: str,
    accumulator: _SearchAccumulator,
    *,
    complete: bool,
    message: str,
) -> TrajectorySearchResult:
    ranked = accumulator.ranked()
    maximum = min(accumulator.limit, len(ranked))

    def build(count: int) -> TrajectorySearchResult:
        records = deterministic_record_order(record for record, _score in ranked[:count])
        return TrajectorySearchResult(
            query=query,
            records=records,
            scanned_records=accumulator.scanned,
            matched_records=accumulator.matched,
            complete=complete,
            truncated=accumulator.matched > count,
            message=message,
        )

    low = 0
    high = maximum
    best = 0
    while low <= high:
        count = (low + high) // 2
        if wire_bytes(build(count).to_wire()) <= TRAJECTORY_RESPONSE_MAX_BYTES:
            best = count
            low = count + 1
        else:
            high = count - 1
    result = build(best)
    if best < maximum and not result.truncated:
        result = replace(result, truncated=True)
    return result


__all__ = ["search_history"]
