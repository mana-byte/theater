"""Independent Unified Vibe history reads and cursor projection."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from theater.constants.trajectory import TRAJECTORY_PAGE_RECORD_LIMIT
from theater.harness.contracts.events import Event
from theater.harness.contracts.source import History, HistoryPage
from theater.harness.contracts.trajectory import ParsedRecord, TrajectoryFact
from theater.transcript_identity import TRANSCRIPT_SOURCE_UNAVAILABLE_CODE

from .unified_projection import project_unified_entry
from .unified_store import UnifiedStoreError, UnifiedStoreView

if TYPE_CHECKING:
    from .unified_source import UnifiedVibeSource


def _nonnegative_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _entries(view: UnifiedStoreView) -> list[dict]:
    history = view.snapshot.get("history")
    values = history.get("entries") if isinstance(history, dict) else None
    return (
        [entry for entry in values if isinstance(entry, dict)] if isinstance(values, list) else []
    )


def _runtime_metadata(view: UnifiedStoreView) -> dict:
    value = view.runtime_state.get("session_metadata")
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True, slots=True)
class _HistoryWindow:
    view: UnifiedStoreView
    end: int
    pinned: bool


@dataclass(frozen=True, slots=True)
class _HistorySelection:
    start: int
    events: list[Event]
    facts: list[TrajectoryFact]


def _cursor(payload: dict) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(body).decode().rstrip("=")


def _decode_cursor(raw: str) -> dict | None:
    try:
        body = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        value = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict) or value.get("version") != 1:
        return None
    return value


class UnifiedHistoryReader:
    """Read Vibe history without changing the source's live checkpoint."""

    def __init__(self, source: UnifiedVibeSource) -> None:
        self._source = source
        self._location: Path | None = None

    def reset(self) -> None:
        self._location = None

    async def _history_view(self) -> tuple[UnifiedStoreView | None, bool, str | None]:
        pinned = self._source._known_location is not None
        path = self._source.path or self._source._known_location or self._location
        if path is None:
            path = await self._source._observer.find_unified_transcript_async(
                cwd=self._source._cwd,
                session_id=self._source._session_id,
                after=self._source._after,
            )
        if path is None:
            return None, pinned, None
        try:
            view = await self._source._load_current(path)
        except (OSError, UnifiedStoreError, ValueError) as exc:
            return None, pinned, str(exc)
        if view is not None:
            self._location = view.current
            self._source._session_id = view.session_id
        return view, pinned, None

    async def history(self, *, last_n: int) -> History:
        view, pinned, error = await self._history_view()
        if view is None:
            return History(
                error_code=TRANSCRIPT_SOURCE_UNAVAILABLE_CODE if error else None,
                error=error,
                pinned=pinned,
            )
        events, _facts = self._project_history(view, 0, len(_entries(view)), clip_text=False)
        return History(
            location=str(view.current),
            events=events[-last_n:] if last_n > 0 else events,
            correlation=self._source._correlation(view.current, view.session_id),
            collision_domain=self._source.collision_domain,
            pinned=pinned,
        )

    async def history_page(
        self,
        *,
        before: str | None = None,
        snapshot: str | None = None,
        limit: int = TRAJECTORY_PAGE_RECORD_LIMIT,
        include_full_text: bool = False,
    ) -> HistoryPage:
        request = self._history_request(before, snapshot, limit)
        if isinstance(request, HistoryPage):
            return request
        bounded_limit, payload = request
        window = await self._history_window(payload)
        if isinstance(window, HistoryPage):
            return window
        view, end, pinned = window.view, window.end, window.pinned
        durable_usage = (
            self._source._durable_usage_fact(view, previous=None) if before is None else None
        )
        selection = self._select_history_records(
            view, end, bounded_limit, reserve_fact=durable_usage is not None, pinned=pinned
        )
        if isinstance(selection, HistoryPage):
            return selection
        facts = selection.facts
        if durable_usage is not None:
            facts.append(durable_usage)
        full_events = None
        if include_full_text:
            full_events, _ = self._project_history(view, selection.start, end, clip_text=False)
        return self._history_page_result(
            view,
            end,
            selection.start,
            selection.events,
            facts,
            full_events,
            pinned,
        )

    @staticmethod
    def _history_request(
        before: str | None, snapshot: str | None, limit: int
    ) -> tuple[int, dict | None] | HistoryPage:
        if type(limit) is not int or limit <= 0:
            return HistoryPage(
                error_code="invalid_limit", error="history page limit must be positive"
            )
        if before is not None and snapshot is not None:
            return HistoryPage(
                error_code="history_cursor_invalid",
                error="history page accepts either an older cursor or a snapshot cursor",
            )
        raw_cursor = before if before is not None else snapshot
        payload = _decode_cursor(raw_cursor) if raw_cursor is not None else None
        if raw_cursor is not None and payload is None:
            return HistoryPage(error_code="history_cursor_invalid", error="invalid history cursor")
        return min(limit, TRAJECTORY_PAGE_RECORD_LIMIT), payload

    async def _history_window(self, payload: dict | None) -> _HistoryWindow | HistoryPage:
        pinned = self._source._known_location is not None
        if payload is None:
            view, pinned, error = await self._history_view()
            if view is None:
                return HistoryPage(
                    error_code=TRANSCRIPT_SOURCE_UNAVAILABLE_CODE if error else None,
                    error=error,
                    pinned=pinned,
                )
            return _HistoryWindow(view, len(_entries(view)), pinned)
        return await self._history_cursor_window(payload, pinned)

    async def _history_cursor_window(
        self, payload: dict, pinned: bool
    ) -> _HistoryWindow | HistoryPage:
        path = self._source.path or self._source._known_location or self._location
        if path is None or payload.get("session_id") != self._source._session_id:
            return HistoryPage(
                error_code="history_cursor_invalid", error="history cursor session mismatch"
            )
        try:
            view = await self._source._load_recovery(
                path,
                at_sequence=_nonnegative_int(payload.get("sequence")),
                generation_hint=payload.get("generation"),
            )
        except (OSError, UnifiedStoreError, ValueError) as exc:
            return HistoryPage(error_code="history_cursor_invalid", error=str(exc), pinned=pinned)
        if view is None or view.watermark != payload.get("watermark"):
            return HistoryPage(
                error_code="history_snapshot_expired",
                error="Unified Vibe history snapshot is no longer retained",
                pinned=pinned,
            )
        end = _nonnegative_int(payload.get("before"))
        if end is None or end > len(_entries(view)):
            return HistoryPage(
                error_code="history_cursor_invalid", error="history cursor boundary is invalid"
            )
        return _HistoryWindow(view, end, pinned)

    def _select_history_records(
        self,
        view: UnifiedStoreView,
        end: int,
        limit: int,
        *,
        reserve_fact: bool,
        pinned: bool,
    ) -> _HistorySelection | HistoryPage:
        selected_start = end
        event_count = fact_count = 0
        indexed = {row[2]: row for row in self._source._rows(view)}
        selected: list[ParsedRecord] = []
        for row_start in range(end - 1, -1, -1):
            row = indexed.get(row_start)
            parsed = (
                self._project_history_entry(view, row, clip_text=True)
                if row is not None
                else ParsedRecord()
            )
            row_events, row_facts = parsed.events, parsed.trajectory
            row_too_large = len(row_events) > limit or len(row_facts) > limit
            would_overflow = (
                event_count + len(row_events) > limit
                or fact_count + len(row_facts) + int(reserve_fact) > limit
            )
            if row_too_large:
                if selected_start == end:
                    return HistoryPage(
                        error_code="history_record_too_large",
                        error="one Unified Vibe entry exceeds the history page limit",
                        pinned=pinned,
                    )
                break
            if would_overflow:
                break
            selected_start = row_start
            selected.append(parsed)
            event_count += len(row_events)
            fact_count += len(row_facts)
        events = [event for parsed in reversed(selected) for event in parsed.events]
        facts = [fact for parsed in reversed(selected) for fact in parsed.trajectory]
        return _HistorySelection(selected_start, events, facts)

    def _history_page_result(
        self,
        view: UnifiedStoreView,
        end: int,
        start: int,
        events: list[Event],
        facts: list[TrajectoryFact],
        full_events: list[Event] | None,
        pinned: bool,
    ) -> HistoryPage:
        base = {
            "version": 1,
            "session_id": view.session_id,
            "generation": view.generation,
            "sequence": view.sequence,
            "watermark": view.watermark,
        }
        snapshot_cursor = _cursor({**base, "before": end})
        older = _cursor({**base, "before": start}) if start > 0 else None
        return HistoryPage(
            location=str(view.current),
            events=events,
            complete_events=full_events,
            trajectory=facts,
            trajectory_events=(),
            cursor=snapshot_cursor,
            snapshot_cursor=snapshot_cursor,
            older_cursor=older,
            has_older=start > 0,
            provenance=self._source._correlation(view.current, view.session_id),
            collision_domain=self._source.collision_domain,
            pinned=pinned,
        )

    def _project_history(
        self, view: UnifiedStoreView, start: int, end: int, *, clip_text: bool
    ) -> tuple[list[Event], list[TrajectoryFact]]:
        events: list[Event] = []
        facts: list[TrajectoryFact] = []
        for row in self._source._rows(view):
            if row[2] >= end:
                break
            if row[2] < start:
                continue
            parsed = self._project_history_entry(view, row, clip_text=clip_text)
            events.extend(parsed.events)
            facts.extend(parsed.trajectory)
        return events, facts

    def _project_history_entry(
        self, view: UnifiedStoreView, row: tuple[str, dict, int], *, clip_text: bool
    ) -> ParsedRecord:
        identity, entry, index = row
        cwd = _runtime_metadata(view).get("cwd")
        return project_unified_entry(
            entry,
            identity=identity,
            index=index,
            watermark=view.watermark,
            source_sequence=view.sequence,
            cwd=cwd if isinstance(cwd, str) else self._source._cwd,
            clip_text=clip_text,
        )
