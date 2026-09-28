"""Follow mutable 2.x messages with a keyset cursor and bounded same-ms rereads."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field

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
#: (cursor, boundary) — the window sweep persisted across reads behind the forward cursor.
type _Sweep = tuple[tuple[int, int], tuple[int, int]]


@dataclass
class _AttachmentSeed:
    point: tuple[int, int]
    count: int
    cursor: tuple[int, int]
    open_cursor: int = -1
    window_done: bool = False
    open_done: bool = False
    fingerprints: dict[str, _Fingerprint] = field(default_factory=dict)
    tools: dict[str, str] = field(default_factory=dict)
    said: set[str] = field(default_factory=set)


class OpenCodeV2Source(OpenCodeSource):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._point: tuple[int, int] = (0, -1)
        self._emitted: dict[str, _Fingerprint] = {}
        self._reread: _Sweep | None = None
        self._seeding: _AttachmentSeed | None = None
        self._staged: _AttachmentSeed | None = None

    def commit_attachment(self) -> None:
        staged = self._staged
        super().commit_attachment()
        self._staged = None
        if staged is not None:
            self._point, self._emitted = staged.point, staged.fingerprints
            self._reread = None
            self._tools, self._said = staged.tools, staged.said

    def discard_attachment(self) -> None:
        super().discard_attachment()
        self._staged = None

    async def aclose(self) -> None:
        await super().aclose()
        self._seeding = None

    def _clear_session_state(self, *, detach: bool) -> None:
        super()._clear_session_state(detach=detach)
        if detach:
            self._cancel_seed()
            self._point = (0, -1)
            self._emitted = {}
            self._reread = None
            self._staged = None

    def _attach(self, conn: sqlite3.Connection, sid: str) -> Batch:
        try:
            if self._seeding is None:
                # Native uses WAL: retain one read snapshot without blocking its writer.
                conn.execute("BEGIN")
                mark, seq, count = head_point(conn, sid)
                self._seeding = _AttachmentSeed(
                    (mark, seq), count, (max(0, mark - _REREAD_WINDOW_MS), -1)
                )
            seed = self._seeding
            self._seed_page(conn, sid, seed)
            if not (seed.window_done and seed.open_done):
                return Batch(has_more=True)
            status = self._status(conn, sid)
            conn.rollback()
        except BaseException:
            self._cancel_seed()
            if conn.in_transaction:
                conn.rollback()
            raise
        self._seeding = None
        self._staged = seed
        self._pending = (sid, self._cursor)
        return Batch(
            attached=Attachment(
                location=f"opencode://{sid}",
                session_id=sid,
                skipped=seed.count,
                correlation=self._attachment_provenance(sid),
            ),
            status=status,
        )

    def _cancel_seed(self) -> None:
        if self._seeding is not None and self._conn is not None:
            self._conn.rollback()
        self._seeding = None

    def _seed_page(self, conn: sqlite3.Connection, sid: str, seed: _AttachmentSeed) -> None:
        """Build baseline maps before attachment, at most two bounded pages per call."""
        if not seed.window_done:
            floor = max(0, seed.point[0] - _REREAD_WINDOW_MS)
            rows = reread_rows(conn, sid, floor, seed.cursor, seed.point, DRAIN_LIMIT + 1)
            seed.window_done = len(rows) <= DRAIN_LIMIT
            for row in rows[:DRAIN_LIMIT]:
                seed.fingerprints[row[0]] = _fingerprint(row)
                seed.cursor = (row[4], row[3])
        if seed.open_done:
            return
        rows = open_turn_rows(conn, sid, after_seq=seed.open_cursor, limit=DRAIN_LIMIT + 1)
        seed.open_done = len(rows) <= DRAIN_LIMIT
        rows = rows[:DRAIN_LIMIT]
        for row in rows:
            seed.open_cursor = row[3]
            for part in row_parts(row):
                if part.get("type") == "tool":
                    status = _table(part.get("state")).get("status")
                    if status and status != "pending":
                        seed.tools[part.get("callID") or part.get("id") or ""] = status
                elif row[2] == "user":
                    seed.said.add(part.get("id") or "")

    def _drain(self, conn: sqlite3.Connection) -> Batch:
        sid = self._session or ""
        events: list = []
        trajectory: list[TrajectoryFact] = []
        fresh = self._reread_page(conn, sid, events, trajectory)
        rows = forward_rows(conn, sid, self._point, DRAIN_LIMIT + 1)
        forward_more = len(rows) > DRAIN_LIMIT
        page = rows[:DRAIN_LIMIT]
        fresh += self._emit(conn, page, events, trajectory)
        if page:
            self._point = (page[-1][4], page[-1][3])
        anchor = self._reread[1][0] if self._reread is not None else self._point[0]
        floor = max(0, anchor - _REREAD_WINDOW_MS)
        self._emitted = {key: seen for key, seen in self._emitted.items() if seen[0] >= floor}
        trajectory.extend(self._refresh_mcp_trajectory())
        has_more = forward_more or self._reread is not None
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

    def _reread_page(
        self, conn: sqlite3.Connection, sid: str, events: list, trajectory: list
    ) -> int:
        """Service older rewrites before advancing the forward cursor's retention window."""
        sweep = self._reread
        if sweep is None:
            sweep = ((max(0, self._point[0] - _REREAD_WINDOW_MS), -1), self._point)
        cursor, boundary = sweep
        floor = max(0, boundary[0] - _REREAD_WINDOW_MS)
        page = reread_rows(conn, sid, floor, cursor, boundary, DRAIN_LIMIT)
        if not page:
            self._reread = None
            return 0
        fresh = self._emit(conn, page, events, trajectory)
        cursor = (page[-1][4], page[-1][3])
        self._reread = (
            (cursor, boundary) if len(page) >= DRAIN_LIMIT and cursor < boundary else None
        )
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
