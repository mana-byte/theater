"""OpenCode 2.x source: follows `session_message` updates, since 2.x persists no event log.

Each changed row is replayed as the 1.x `message.part.updated` / `message.updated` events the
parser already decodes. A `(time_updated, seq)` cursor keeps batches moving forward when many
rows share one timestamp; a short window behind it is re-read because `time_updated` is in ms.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator

from theater.harness.contracts.trajectory import TrajectoryFact
from theater.harness.source import Attachment, Batch
from theater.models import Status

from .constants import DRAIN_LIMIT
from .source import OpenCodeSource
from .store_v2 import (
    Row,
    forward_rows,
    head_point,
    latest_row,
    open_turn_rows,
    reread_rows,
    row_info,
    row_parts,
)
from .values import _has_tool_calls, _table, _terminal_finish

#: Two updates of one row inside a millisecond share `time_updated`; content decides instead.
_REREAD_WINDOW_MS = 2_000

type _Fingerprint = tuple[int, bytes]


class OpenCodeV2Source(OpenCodeSource):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._point: tuple[int, int] = (0, -1)
        self._emitted: dict[str, _Fingerprint] = {}
        self._staged: tuple[tuple[int, int], dict[str, _Fingerprint], list[Row]] | None = None

    def commit_attachment(self) -> None:
        staged = self._staged
        super().commit_attachment()
        self._staged = None
        if staged is not None:
            self._point, self._emitted, open_turn = staged
            self._seed(open_turn)

    def discard_attachment(self) -> None:
        super().discard_attachment()
        self._staged = None

    def _clear_session_state(self, *, detach: bool) -> None:
        super()._clear_session_state(detach=detach)
        if detach:
            self._point = (0, -1)
            self._emitted = {}
            self._staged = None

    def _attach(self, conn: sqlite3.Connection, sid: str) -> Batch:
        mark, seq, count = head_point(conn, sid)
        point = (mark, seq)
        seeded = {
            row[0]: _fingerprint(row)
            for page in self._window_pages(conn, sid, point)
            for row in page
        }
        self._staged = (point, seeded, open_turn_rows(conn, sid))
        self._pending = (sid, self._cursor)
        return Batch(
            attached=Attachment(
                location=f"opencode://{sid}",
                session_id=sid,
                skipped=count,
                correlation=self._attachment_provenance(sid),
            ),
            status=self._status(conn, sid),
        )

    def _window_pages(
        self, conn: sqlite3.Connection, sid: str, point: tuple[int, int]
    ) -> Iterator[list[Row]]:
        """Bounded newest-first pages of the reread window at or behind `point`."""
        floor = max(0, point[0] - _REREAD_WINDOW_MS)
        boundary = point
        inclusive = True
        while True:
            page = reread_rows(conn, sid, floor, boundary, DRAIN_LIMIT, inclusive=inclusive)
            if not page:
                return
            yield page
            if len(page) < DRAIN_LIMIT:
                return
            boundary = (page[-1][4], page[-1][3])
            inclusive = False

    def _seed(self, rows: list[Row]) -> None:
        """Count the in-flight turn's settled tools and prompts as already reported."""
        for row in rows:
            for part in row_parts(row):
                if part.get("type") == "tool":
                    status = _table(part.get("state")).get("status")
                    if status and status != "pending":
                        self._tools[part.get("callID") or part.get("id") or ""] = status
                elif row[2] == "user":
                    self._said.add(part.get("id") or "")

    def _drain(self, conn: sqlite3.Connection) -> Batch:
        sid = self._session or ""
        events: list = []
        trajectory: list[TrajectoryFact] = []
        rows = forward_rows(conn, sid, self._point, DRAIN_LIMIT + 1)
        has_more = len(rows) > DRAIN_LIMIT
        page = rows[:DRAIN_LIMIT]
        fresh = self._emit(conn, page, events, trajectory)
        if page:
            self._point = (page[-1][4], page[-1][3])
        if not has_more:
            fresh += self._reread(conn, sid, events, trajectory)
        floor = max(0, self._point[0] - _REREAD_WINDOW_MS)
        self._emitted = {key: seen for key, seen in self._emitted.items() if seen[0] >= floor}
        trajectory.extend(self._refresh_mcp_trajectory())
        if not fresh:
            return Batch(
                has_more=has_more,
                trajectory=trajectory,
                trajectory_events=() if trajectory else None,
            )
        return Batch(
            events=events,
            progressed=True,
            has_more=has_more,
            trajectory=trajectory,
            trajectory_events=(),
        )

    def _reread(self, conn: sqlite3.Connection, sid: str, events: list, trajectory: list) -> int:
        """Same-ms rewrites keep a row's position; page the window behind the cursor."""
        fresh = 0
        for page in self._window_pages(conn, sid, self._point):
            fresh += self._emit(conn, page, events, trajectory)
        return fresh

    def _emit(
        self, conn: sqlite3.Connection, rows: list[Row], events: list, trajectory: list
    ) -> int:
        """Translate rows whose fingerprint moved since they were last reported."""
        fresh = 0
        for row in rows:
            seen = _fingerprint(row)
            if self._emitted.get(row[0]) == seen:
                continue
            fresh += 1
            for kind, payload in self._row_events(conn, row):
                self._cursor += 1
                translated, facts = self._translate_with_trajectory(
                    conn, kind, payload, self._cursor
                )
                events.extend(translated)
                trajectory.extend(facts)
            self._emitted[row[0]] = seen
        return fresh

    def _row_events(self, conn: sqlite3.Connection, row: Row) -> list[tuple[str, dict]]:
        """A row as the 1.x events it replaces: an assistant's parts land before its finish."""
        message = ("message.updated.1", {"info": row_info(conn, row)})
        parts = [
            ("message.part.updated.1", {"part": part, "time": row[4]}) for part in row_parts(row)
        ]
        return [*parts, message] if row[2] == "assistant" else [message, *parts]

    def _status(self, conn: sqlite3.Connection, sid: str) -> Status:
        row = latest_row(conn, sid)
        if row is None or row[2] == "idle":
            return Status.IDLE
        if row[2] != "assistant":
            return Status.WORKING
        info = row_info(conn, row)
        if info.get("error") or (
            _terminal_finish(info.get("finish"))
            and _table(info.get("time")).get("completed")
            and not _has_tool_calls(row_parts(row))
        ):
            return Status.IDLE
        return Status.WORKING


def _fingerprint(row: Row) -> _Fingerprint:
    return row[4], hashlib.blake2b(row[5].encode("utf-8"), digest_size=12).digest()


__all__ = ["OpenCodeV2Source"]
