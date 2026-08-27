"""OpenCode source lifecycle and attachment composition.

The shared live database stays read-only and non-immutable for the source lifetime.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path

from theater.harness.contracts.trajectory import TrajectoryFact
from theater.harness.source import Attachment, Batch, ReceiptAdmission, Source, SourceContractError
from theater.models import Status
from theater.provenance import TranscriptProvenance, normalize_provenance

from .constants import CORRELATION_READY_TIMEOUT, STEP_FINISH
from .history import OpenCodeHistory
from .identity import OpenCodeIdentity
from .parser import OpenCodeParser
from .store import event_head, latest_message, open_readonly
from .trajectory import OpenCodeTrajectory
from .values import _table, load_json_object

logger = logging.getLogger("theater.harness.opencode")


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
        self._session: str | None = None
        self._cursor = -1
        self._pending: tuple[str, int] | None = None
        self._roles: dict[str, str] = {}
        self._text: dict[str, dict[str, str]] = {}
        self._tools: dict[str, str] = {}
        self._stamp: dict[str, float] = {}
        self._finished: set[str] = set()
        self._said: set[str] = set()
        self._trajectory_revisions: dict[str, int] = {}
        self._trajectory_signatures: dict[str, TrajectoryFact] = {}

    async def read(self) -> Batch:
        return await asyncio.to_thread(self._read)

    def commit_attachment(self) -> None:
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
        self._clear_session_state(detach=False)

    def discard_attachment(self) -> None:
        if self._pending is None:
            raise RuntimeError("no opencode attachment is pending")
        self._pending = None

    def revoke_attachment(self) -> None:
        self._pending = None
        self._session_id = None
        self._session_provenance = TranscriptProvenance.HEURISTIC
        self._session_exact = False
        self._known_location = None
        self._known_location_provenance = TranscriptProvenance.HEURISTIC
        self._receipt_target = None
        self._clear_session_state(detach=True)

    def admit_exact_location(self, *, location: str, session_id: str) -> ReceiptAdmission:
        if not isinstance(session_id, str) or not session_id.strip():
            raise SourceContractError("OpenCode receipt session_id must be a nonblank string")
        if "://" in session_id:
            raise SourceContractError("OpenCode receipt session_id must be a native session id")
        if location != f"opencode://{session_id}":
            raise SourceContractError("OpenCode receipt location must match its session_id exactly")
        self._pending = None
        self._session_id = session_id
        self._session_provenance = TranscriptProvenance.EXACT
        self._session_exact = True
        self._known_location = location
        self._known_location_provenance = TranscriptProvenance.EXACT
        self._receipt_expected = False
        self._receipt_deadline = None
        if self._session == session_id:
            self._receipt_target = None
            return "accepted"
        self._receipt_target = session_id
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
        self._said.clear()
        self._trajectory_revisions.clear()
        self._trajectory_signatures.clear()

    async def aclose(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                logger.debug("closing the opencode database failed", exc_info=True)

    def _read(self) -> Batch:
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
                    return Batch(waiting=True)
                if self._trusted_known_location():
                    return self._identity_lost_batch(
                        f"trusted transcript pin {self._known_location!r} no longer exists"
                    )
                return self._correlation_problem() or Batch(waiting=True)
            return self._drain(conn)
        except sqlite3.Error as exc:
            logger.debug("reading the opencode database failed", exc_info=True)
            return self._source_unavailable_batch(f"reading OpenCode database failed: {exc}")

    def _open(self) -> sqlite3.Connection | None:
        if self._conn is None:
            self._conn = open_readonly(self._db, persistent=True)
        return self._conn

    def _attach(self, conn: sqlite3.Connection, sid: str) -> Batch:
        row = event_head(conn, sid)
        status = self._status(conn, sid)
        self._pending = (sid, row[0])
        return Batch(
            attached=Attachment(
                location=f"opencode://{sid}",
                session_id=sid,
                skipped=row[1],
                correlation=self._attachment_provenance(sid),
            ),
            status=status,
        )

    def _status(self, conn: sqlite3.Connection, sid: str) -> Status:
        row = latest_message(conn, sid)
        if row is None:
            return Status.IDLE
        info = load_json_object(row[0])
        if info.get("role") != "assistant":
            return Status.WORKING
        time_data = _table(info.get("time"))
        finish = info.get("finish")
        if finish and finish != STEP_FINISH and time_data.get("completed"):
            return Status.IDLE
        return Status.WORKING
