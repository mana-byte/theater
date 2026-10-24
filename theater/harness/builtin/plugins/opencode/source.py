"""OpenCode source lifecycle and attachment composition.

The shared live database stays read-only and non-immutable for the source lifetime.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from theater.harness.contracts.trajectory import TrajectoryFact
from theater.harness.source import Attachment, Batch, ReceiptAdmission, Source, SourceContractError
from theater.models import Status
from theater.provenance import TranscriptProvenance, normalize_provenance

from .constants import CORRELATION_READY_TIMEOUT, DRAIN_LIMIT
from .history import OpenCodeHistory
from .identity import OpenCodeIdentity, validate_receipt_session_id
from .mcp import OpenCodeMcpCatalog
from .parser import OpenCodeParser
from .queries import (
    Row,
    forward_rows,
    head_point,
    latest_row,
    open_turn_rows,
    reread_rows,
    row_info,
    row_parts,
)
from .store import open_readonly
from .trajectory import OpenCodeTrajectory
from .values import _has_tool_calls, _table, _terminal_finish

logger = logging.getLogger("theater.harness.opencode")


#: Same-ms updates need a content fingerprint and a bounded reread window.
_REREAD_WINDOW_MS = 2_000
type _Fingerprint = tuple[int, bytes]
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


class OpenCodeSource(OpenCodeHistory, OpenCodeParser, OpenCodeTrajectory, OpenCodeIdentity, Source):
    def __init__(
        self,
        db: Path,
        *,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
        receipt_expected: bool = False,
        session_provenance: str | TranscriptProvenance | None = None,
        known_location: str | None = None,
        mcp_catalog_path: Path | None = None,
    ) -> None:
        self._db = db
        self._cwd = cwd
        self._session_id = session_id
        self._session_provenance = normalize_provenance(session_provenance)
        self._session_exact = self._session_provenance is TranscriptProvenance.EXACT
        self._known_location = known_location
        self._known_location_provenance = (
            self._session_provenance
            if self._known_location is not None
            else TranscriptProvenance.HEURISTIC
        )
        self._after = after
        self._receipt_expected = receipt_expected
        self._receipt_deadline = (
            time.monotonic() + CORRELATION_READY_TIMEOUT if receipt_expected else None
        )
        self._receipt_target: str | None = None
        self._conn: sqlite3.Connection | None = None
        self._connection_lock = threading.Lock()
        self._closed = False
        self._session: str | None = None
        self._cursor = -1
        self._pending: tuple[str, int] | None = None
        self._roles: dict[str, str] = {}
        self._text: dict[str, dict[str, str]] = {}
        self._tools: dict[str, str] = {}
        self._stamp: dict[str, float] = {}
        self._finished: set[str] = set()
        self._snapshotted: set[str] = set()
        self._step_failed: set[str] = set()
        self._said: set[str] = set()
        self._trajectory_state: OrderedDict[str, tuple[int, TrajectoryFact]] = OrderedDict()
        self._mcp_catalog = OpenCodeMcpCatalog(mcp_catalog_path)
        self._mcp_catalog_generation = self._mcp_catalog.generation
        self._point: tuple[int, int] = (0, -1)
        self._emitted: dict[str, _Fingerprint] = {}
        self._reread: _Sweep | None = None
        self._seeding: _AttachmentSeed | None = None
        self._staged: _AttachmentSeed | None = None

    async def read(self) -> Batch:
        return await asyncio.to_thread(self._read)

    def commit_attachment(self) -> None:
        staged = self._staged
        if self._pending is None:
            raise RuntimeError("no opencode attachment is pending")
        session, cursor = self._pending
        provenance = normalize_provenance(self._attachment_provenance(session))
        self._session, self._cursor = session, cursor
        self._session_id = self._session
        self._known_location = f"opencode://{self._session}"
        self._session_provenance = provenance
        self._session_exact = provenance is TranscriptProvenance.EXACT
        self._known_location_provenance = provenance
        self._pending = None
        if self._receipt_target == session:
            self._receipt_target = None
            self._receipt_deadline = None
        self._clear_session_state(detach=False)
        self._staged = None
        if staged is not None:
            self._point, self._emitted = staged.point, staged.fingerprints
            self._reread = None
            self._tools, self._said = staged.tools, staged.said

    def discard_attachment(self) -> None:
        if self._pending is None:
            raise RuntimeError("no opencode attachment is pending")
        self._pending = None
        self._staged = None

    def revoke_attachment(self) -> None:
        self._pending = None
        self._session_id = None
        self._session_provenance = TranscriptProvenance.HEURISTIC
        self._session_exact = False
        self._known_location = None
        self._known_location_provenance = TranscriptProvenance.HEURISTIC
        self._receipt_target = None
        self._receipt_deadline = None
        self._clear_session_state(detach=True)

    def admit_exact_location(self, *, location: str, session_id: str) -> ReceiptAdmission:
        try:
            session_id = validate_receipt_session_id(session_id)
        except ValueError as exc:
            raise SourceContractError(str(exc)) from exc
        if location != f"opencode://{session_id}":
            raise SourceContractError("OpenCode receipt location must match its session_id exactly")
        self._pending = None
        self._session_id = session_id
        self._session_provenance = TranscriptProvenance.EXACT
        self._session_exact = True
        self._known_location = location
        self._known_location_provenance = TranscriptProvenance.EXACT
        self._receipt_expected = False
        if self._session == session_id:
            self._receipt_target = None
            self._receipt_deadline = None
            return "accepted"
        if self._receipt_target == session_id:
            return "staged"
        self._receipt_target = session_id
        self._receipt_deadline = time.monotonic() + CORRELATION_READY_TIMEOUT
        self._clear_session_state(detach=True)
        return "staged"

    def _clear_session_state(self, *, detach: bool) -> None:
        if detach:
            self._session = None
            self._cursor = -1
        self._roles.clear()
        self._text.clear()
        self._tools.clear()
        self._stamp.clear()
        self._finished.clear()
        self._snapshotted.clear()
        self._step_failed.clear()
        self._said.clear()
        self._trajectory_state.clear()
        if detach:
            self._cancel_seed()
            self._point = (0, -1)
            self._emitted = {}
            self._reread = None
            self._staged = None

    async def aclose(self) -> None:
        # Cancelling to_thread does not stop SQLite; close only after its worker releases the lock.
        self._closed = True
        await asyncio.to_thread(self._close_connection)
        self._seeding = None

    def _close_connection(self) -> None:
        with self._connection_lock:
            conn, self._conn = self._conn, None
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    logger.debug("closing the opencode database failed", exc_info=True)

    def _read(self) -> Batch:
        with self._connection_lock:
            if self._closed:
                return self._source_unavailable_batch("OpenCode source is closed")
            return self._read_connection()

    def _read_connection(self) -> Batch:
        self._require_decision()
        conn = self._open()
        if conn is None:
            return self._source_unavailable_batch("OpenCode database is unavailable")
        try:
            if self._session is None:
                found = self._locate(conn, pinned=True)
                if found:
                    return self._attach(conn, found)
                if self._receipt_target is not None:
                    return self._correlation_problem(conn) or Batch(waiting=True)
                if self._trusted_known_location():
                    return self._identity_lost_batch(
                        f"trusted transcript pin {self._known_location!r} no longer exists"
                    )
                return self._correlation_problem(conn) or Batch(waiting=True)
            return self._drain(conn)
        except sqlite3.Error as exc:
            logger.debug("reading the opencode database failed", exc_info=True)
            return self._source_unavailable_batch(f"reading OpenCode database failed: {exc}")

    def _open(self) -> sqlite3.Connection | None:
        if self._conn is None:
            self._conn = open_readonly(self._db, persistent=True)
        return self._conn

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
            conn.rollback()
            status = self._status(conn, sid)
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
            has_more=True,
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
        """Ordered observation events: an assistant's parts land before its finish."""
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
        # A failed step keeps retrying inside the turn; only the idle marker ends it.
        if (
            _terminal_finish(info.get("finish"))
            and _table(info.get("time")).get("completed")
            and not _has_tool_calls(row_parts(row))
        ):
            return Status.IDLE
        return Status.WORKING


def _fingerprint(row: Row) -> _Fingerprint:
    return row[4], hashlib.blake2b(row[5].encode("utf-8"), digest_size=12).digest()


__all__ = ["OpenCodeSource"]
