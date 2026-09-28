"""OpenCode 2.x source: follows `session_message` updates, since 2.x persists no event log.

Each changed row is replayed as the 1.x `message.part.updated` / `message.updated` events the
parser already decodes; rows are re-read over a short window because `time_updated` is in ms.
"""

from __future__ import annotations

import hashlib
import sqlite3

from theater.harness.contracts.trajectory import TrajectoryFact
from theater.harness.source import Attachment, Batch
from theater.models import Status

from .constants import DRAIN_LIMIT
from .source import OpenCodeSource
from .store_v2 import Row, changed_rows, head, latest_row, open_turn_rows, row_info, row_parts
from .values import _has_tool_calls, _table, _terminal_finish

#: Two updates of one row inside a millisecond share `time_updated`; content decides instead.
_REREAD_WINDOW_MS = 2_000

type _Fingerprint = tuple[int, bytes]


class OpenCodeV2Source(OpenCodeSource):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._since = 0
        self._emitted: dict[str, _Fingerprint] = {}
        self._staged: tuple[int, dict[str, _Fingerprint], list[Row]] | None = None

    def commit_attachment(self) -> None:
        staged = self._staged
        super().commit_attachment()
        self._staged = None
        if staged is not None:
            self._since, self._emitted, open_turn = staged
            self._seed(open_turn)

    def discard_attachment(self) -> None:
        super().discard_attachment()
        self._staged = None

    def _clear_session_state(self, *, detach: bool) -> None:
        super()._clear_session_state(detach=detach)
        if detach:
            self._since = 0
            self._emitted = {}
            self._staged = None

    def _attach(self, conn: sqlite3.Connection, sid: str) -> Batch:
        mark, count, _ids = head(conn, sid)
        recent = changed_rows(conn, sid, max(0, mark - _REREAD_WINDOW_MS), DRAIN_LIMIT)
        self._staged = (
            mark,
            {row[0]: _fingerprint(row) for row in recent},
            open_turn_rows(conn, sid),
        )
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
        rows = changed_rows(conn, sid, max(0, self._since - _REREAD_WINDOW_MS), DRAIN_LIMIT + 1)
        has_more = len(rows) > DRAIN_LIMIT
        rows = rows[:DRAIN_LIMIT]
        fresh = [row for row in rows if self._emitted.get(row[0]) != _fingerprint(row)]
        if rows:
            self._since = max(self._since, rows[-1][4])
        if not fresh:
            updates = self._refresh_mcp_trajectory()
            return Batch(trajectory=updates, trajectory_events=() if updates else None)
        events = []
        trajectory: list[TrajectoryFact] = []
        for row in fresh:
            for kind, payload in self._row_events(conn, row):
                self._cursor += 1
                translated, facts = self._translate_with_trajectory(
                    conn, kind, payload, self._cursor
                )
                events.extend(translated)
                trajectory.extend(facts)
            self._emitted[row[0]] = _fingerprint(row)
        floor = self._since - _REREAD_WINDOW_MS
        self._emitted = {key: seen for key, seen in self._emitted.items() if seen[0] >= floor}
        trajectory.extend(self._refresh_mcp_trajectory())
        return Batch(
            events=events,
            progressed=True,
            has_more=has_more,
            trajectory=trajectory,
            trajectory_events=(),
        )

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
