"""OpenCode session correlation and discovery."""

# mypy: disable-error-code="attr-defined,operator,return-value"

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

from theater.harness.source import Batch, TranscriptCandidate
from theater.provenance import TranscriptProvenance, is_trusted_provenance
from theater.transcript_identity import (
    TRANSCRIPT_IDENTITY_LOST_CODE,
    TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
)

from .constants import CORRELATION_READY_TIMEOUT
from .store import (
    candidate_session,
    candidate_sessions,
    has_root_session,
    located_sessions,
    open_readonly,
    root_session,
    session,
)

logger = logging.getLogger("theater.harness.opencode")


def transcript_candidates(
    db: Path,
    *,
    cwd: str | None,
    domain: str | None = None,
    after: float | None = None,
) -> list[TranscriptCandidate]:
    if not db.exists() or not cwd:
        return []
    expected_domain = f"opencode://{db.resolve()}"
    if domain is not None and domain != expected_domain:
        return []
    want = str(Path(cwd).resolve())
    try:
        stat = db.stat()
    except OSError:
        stat = None
    conn = open_readonly(db)
    if conn is None:
        return []
    rows: list[TranscriptCandidate] = []
    try:
        for sid, directory, created in candidate_sessions(conn):
            reason = None
            before_floor = (
                after is not None and isinstance(created, (int, float)) and created < after * 1000
            )
            if before_floor:
                reason = "created before participant floor"
            elif directory != want:
                reason = "cwd mismatch"
            rows.append(
                TranscriptCandidate(
                    location=f"opencode://{sid}",
                    session_id=str(sid),
                    mtime=stat.st_mtime if stat else None,
                    size=stat.st_size if stat else None,
                    rejection_reason=reason,
                    domain=expected_domain,
                )
            )
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return rows


def admit_operator_candidate(
    db: Path,
    *,
    cwd: str | None,
    candidate: str,
    domain: str | None = None,
    after: float | None = None,
) -> TranscriptCandidate:
    if not db.exists():
        raise ValueError("OpenCode database does not exist")
    expected_domain = f"opencode://{db.resolve()}"
    if domain is not None and domain != expected_domain:
        raise ValueError("candidate session is outside this harness transcript domain")
    sid = candidate.removeprefix("opencode://")
    if not sid:
        raise ValueError("unextractable session id")
    want = str(Path(cwd).resolve()) if cwd else None
    stat = db.stat()
    conn = open_readonly(db)
    if conn is None:
        raise ValueError("OpenCode database is not readable")
    try:
        try:
            row = candidate_session(conn, sid)
        except sqlite3.Error as exc:
            raise ValueError("OpenCode database is not readable") from exc
    finally:
        conn.close()
    if row is None:
        raise ValueError("harness shape mismatch")
    if want is not None and row[1] != want:
        raise ValueError("cwd mismatch")
    if after is not None and isinstance(row[2], (int, float)) and row[2] < after * 1000:
        raise ValueError("created before participant floor")
    return TranscriptCandidate(
        location=f"opencode://{row[0]}",
        session_id=str(row[0]),
        mtime=stat.st_mtime,
        size=stat.st_size,
        domain=expected_domain,
    )


class OpenCodeIdentity:
    def _locate(self, conn, *, pinned: bool) -> str | None:
        if self._receipt is not None:
            sid = self._read_receipt()
            if sid is None:
                self._located_exact = False
                self._located_receipt_sid = None
                return None
            row = root_session(conn, sid)
            self._located_exact = row is not None
            self._located_receipt_sid = row[0] if row is not None else None
            return row[0] if row is not None else None
        pinned_sid = self._pinned_sid()
        if pinned and pinned_sid is not None and self._trusted_known_location():
            row = root_session(conn, pinned_sid)
            self._located_exact = row is not None
            self._located_receipt_sid = None
            return row[0] if row is not None else None
        if pinned and self._session_id:
            row = session(conn, self._session_id)
            if row is not None:
                self._located_exact = self._session_exact
                self._located_receipt_sid = None
                return row[0]
        if not self._cwd:
            self._located_exact = False
            self._located_receipt_sid = None
            return None
        self._located_exact = False
        self._located_receipt_sid = None
        want = str(Path(self._cwd).resolve())
        count = located_sessions(conn, want, self._after, count=True)
        if count is not None and count[0] > 1:
            logger.warning(
                "opencode _locate: %d sessions match cwd %s; "
                "returning a heuristic candidate for the reducer to validate",
                count[0],
                self._cwd,
            )
        row = located_sessions(conn, want, self._after)
        return row[0] if row is not None else None

    def _read_receipt(self) -> str | None:
        """Read and validate the process-local participant/session receipt."""
        if self._receipt is None or self._participant_id is None:
            return None
        try:
            found = json.loads(self._receipt.read_text())
        except (FileNotFoundError, OSError, ValueError):
            return None
        if not isinstance(found, dict) or found.get("participant_id") != self._participant_id:
            return None
        sid = found.get("session_id")
        return sid if isinstance(sid, str) and sid else None

    def _correlation_problem(self, conn) -> Batch | None:
        """Surface a missing exact channel after bounded startup."""
        if self._receipt is None or self._participant_id is None:
            return None
        if time.monotonic() - self._receipt_started < CORRELATION_READY_TIMEOUT:
            return None
        try:
            found = json.loads(self._receipt.read_text())
        except (FileNotFoundError, OSError, ValueError):
            found = None
        if not isinstance(found, dict) or found.get("participant_id") != self._participant_id:
            return Batch(
                waiting=True,
                error_code="transcript_correlation_failed",
                error="OpenCode's Theater correlation plugin did not initialize",
            )
        if not self._cwd:
            return None
        if not has_root_session(conn, str(Path(self._cwd).resolve()), self._after):
            return None
        return Batch(
            waiting=True,
            error_code="transcript_correlation_failed",
            error="OpenCode created a session but its exact Theater receipt is missing",
        )

    def _pinned_sid(self) -> str | None:
        if self._known_location and self._known_location.startswith("opencode://"):
            return self._known_location.removeprefix("opencode://") or None
        return None

    def _trusted_known_location(self) -> bool:
        return self._pinned_sid() is not None and is_trusted_provenance(
            self._known_location_provenance
        )

    @staticmethod
    def _identity_lost_batch(reason: str) -> Batch:
        return Batch(waiting=True, error_code=TRANSCRIPT_IDENTITY_LOST_CODE, error=reason)

    @staticmethod
    def _source_unavailable_batch(reason: str) -> Batch:
        return Batch(waiting=True, error_code=TRANSCRIPT_SOURCE_UNAVAILABLE_CODE, error=reason)

    @staticmethod
    def _session_exists(conn, sid: str) -> bool:
        return root_session(conn, sid) is not None

    def _attachment_provenance(self, sid: str) -> str:
        if self._known_location == f"opencode://{sid}" and is_trusted_provenance(
            self._known_location_provenance
        ):
            return str(self._known_location_provenance)
        if self._located_receipt_sid == sid:
            return str(TranscriptProvenance.EXACT)
        if self._session_exact and self._session_id == sid:
            return str(TranscriptProvenance.EXACT)
        return str(TranscriptProvenance.HEURISTIC)

    def _require_decision(self) -> None:
        if self._pending is not None:
            raise RuntimeError("attachment must be committed or discarded before reading again")
