"""Bounded OpenCode history replay and cursor validation."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import math
import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from theater.constants.trajectory import (
    TRAJECTORY_CURSOR_MAX_BYTES,
    TRAJECTORY_PAGE_RECORD_LIMIT,
)
from theater.harness.base import Event
from theater.harness.contracts.source import HistoryPage
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.harness.source import Batch, History
from theater.transcript_identity import (
    TRANSCRIPT_IDENTITY_LOST_CODE,
    TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
)

from .store import (
    history_boundary,
    history_messages,
    history_parts_by_session,
    paged_messages,
    paged_parts,
    session_for_history,
)
from .values import _opencode_source_key, load_json_object

logger = logging.getLogger("theater.harness.opencode")


class _OpenCodeHistoryPageError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class OpenCodeHistory:
    _after: float | None
    _cwd: str | None
    _db: Path
    _known_location: str | None
    _receipt: Path | None
    _session: str | None
    _session_id: str | None

    if TYPE_CHECKING:

        def _attachment_provenance(self, sid: str) -> str: ...

        def _correlation_problem(self, conn: sqlite3.Connection) -> Batch | None: ...

        def _locate(self, conn: sqlite3.Connection, *, pinned: bool) -> str | None: ...

        def _open(self) -> sqlite3.Connection | None: ...

        def _pinned_sid(self) -> str | None: ...

        def _read_receipt(self) -> str | None: ...

        def _replay(self, info: dict, parts: list[dict]) -> list[Event]: ...

        def _session_exists(self, conn: sqlite3.Connection, sid: str) -> bool: ...

        def _stored_facts_for_message(
            self,
            info: dict,
            parts: Sequence[tuple[object, object, object, object]],
            *,
            raw_index: int,
            message_revision: int,
        ) -> list[TrajectoryFact]: ...

        def _trusted_known_location(self) -> bool: ...

    async def history(self, *, last_n: int) -> History:
        return await asyncio.to_thread(self._history, last_n)

    async def history_page(
        self,
        *,
        before: str | None = None,
        limit: int = TRAJECTORY_PAGE_RECORD_LIMIT,
    ) -> HistoryPage:
        if type(limit) is not int or limit <= 0:
            return HistoryPage(
                error_code="invalid_limit", error="history page limit must be positive"
            )
        limit = min(limit, TRAJECTORY_PAGE_RECORD_LIMIT)
        return await asyncio.to_thread(self._history_page, before, limit)

    def _history(self, last_n: int) -> History:
        """Replay current message and part rows independently of the live cursor."""
        conn = self._open()
        if conn is None:
            return History(
                error_code=TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
                error="OpenCode database is unavailable",
                pinned=self._trusted_known_location(),
            )
        pinned_sid = None
        if self._known_location and self._known_location.startswith("opencode://"):
            pinned_sid = self._known_location.removeprefix("opencode://") or None
        pinned = self._session is None and pinned_sid is not None
        try:
            if (
                pinned_sid is not None
                and self._trusted_known_location()
                and not self._session_exists(conn, pinned_sid)
            ):
                return History(
                    error_code=TRANSCRIPT_IDENTITY_LOST_CODE,
                    error=f"trusted transcript pin {self._known_location!r} no longer exists",
                    pinned=True,
                )
            sid = self._session or pinned_sid or self._locate(conn, pinned=True)
            if sid is None:
                problem = self._correlation_problem(conn)
                return (
                    History(error_code=problem.error_code, error=problem.error)
                    if problem is not None
                    else History()
                )
            parts: dict[str, list[dict]] = {}
            rows = history_parts_by_session(conn, sid)
            for mid, raw in rows:
                parts.setdefault(mid, []).append(load_json_object(raw))
            events: list[Event] = []
            rows = history_messages(conn, sid)
            for mid, raw in rows:
                events.extend(self._replay(load_json_object(raw), parts.get(mid, [])))
        except sqlite3.Error as exc:
            logger.debug("reading opencode history failed", exc_info=True)
            return History(
                error_code=TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
                error=f"reading OpenCode database failed: {exc}",
                pinned=pinned,
            )
        events = [event for event in events if not event.usage_only]
        if last_n > 0:
            events = events[-last_n:]
        # Stored rows carry no sequence number, so position stands in for one.
        events = [replace(e, raw_index=i) for i, e in enumerate(events)]
        return History(
            location=f"opencode://{sid}",
            events=events,
            correlation=self._attachment_provenance(sid),
            pinned=pinned,
        )

    def _history_page(self, before: str | None, limit: int) -> HistoryPage:
        if not self._db.exists():
            return HistoryPage(
                error_code=TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
                error="OpenCode database is unavailable",
            )
        try:
            conn = sqlite3.connect(f"file:{self._db}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            return HistoryPage(
                error_code=TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
                error=f"reading OpenCode database failed: {exc}",
            )
        try:
            return self._history_page_with_connection(conn, before, limit)
        except sqlite3.Error as exc:
            logger.debug("reading opencode history page failed", exc_info=True)
            return HistoryPage(
                error_code=TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
                error=f"reading OpenCode database failed: {exc}",
            )
        finally:
            conn.close()

    def _history_page_with_connection(  # noqa: PLR0912, PLR0915
        self, conn: sqlite3.Connection, before: str | None, limit: int
    ) -> HistoryPage:
        sid = self._history_session(conn)
        pinned = self._known_location is not None
        if sid is None:
            if before is not None:
                return HistoryPage(
                    error_code="history_cursor_invalid",
                    error="history cursor session is unavailable",
                    pinned=pinned,
                )
            return HistoryPage(pinned=pinned)
        try:
            stat = self._db.stat()
        except OSError as exc:
            return HistoryPage(
                error_code=TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
                error=f"OpenCode database is unavailable: {exc}",
                pinned=pinned,
            )
        identity = {"dev": int(stat.st_dev), "ino": int(stat.st_ino), "size": int(stat.st_size)}
        boundary: tuple[int | float, str, str] | None = None
        if before is not None:
            try:
                cursor = self._decode_history_cursor(before)
                boundary = self._validate_history_cursor(conn, cursor, sid, identity)
            except _OpenCodeHistoryPageError as exc:
                return HistoryPage(
                    error_code=exc.code,
                    error=str(exc),
                    pinned=pinned,
                )
        rows = paged_messages(conn, sid, boundary, limit)
        selected: list[tuple[object, object, object, object]] = []
        selected_output: list[tuple[tuple[Event, ...], tuple[TrajectoryFact, ...], int]] = []
        event_count = 0
        fact_count = 0
        has_more = False
        for row_index, row in enumerate(rows):
            if row_index >= limit:
                has_more = True
                break
            message_id, created, updated, raw = row
            key = self._history_row_key(created, message_id)
            if key is None:
                continue
            parts, parts_truncated = self._history_parts(conn, sid, str(message_id), limit)
            if parts_truncated:
                return HistoryPage(
                    error_code="history_record_too_large",
                    error="one OpenCode message has too many parts for the history page limit",
                    pinned=pinned,
                )
            info = load_json_object(raw)
            if not isinstance(info.get("id"), str):
                info["id"] = str(message_id)
            message_events = tuple(
                event
                for event in self._replay(info, [load_json_object(part[3]) for part in parts])
                if not event.usage_only
            )
            message_facts = tuple(
                self._stored_facts_for_message(
                    info,
                    parts,
                    raw_index=self._history_coordinate(created),
                    message_revision=self._stored_revision(info, updated, created),
                )
            )
            if len(message_events) > limit or len(message_facts) > limit:
                return HistoryPage(
                    error_code="history_record_too_large",
                    error="one OpenCode message exceeds the history page limit",
                    pinned=pinned,
                )
            if event_count + len(message_events) > limit or fact_count + len(message_facts) > limit:
                has_more = True
                break
            selected.append(row)
            selected_output.append(
                (message_events, message_facts, self._history_coordinate(created))
            )
            event_count += len(message_events)
            fact_count += len(message_facts)
        if not selected:
            return HistoryPage(
                location=f"opencode://{sid}",
                pinned=pinned,
                provenance=self._attachment_provenance(sid),
            )
        newest = selected[0]
        oldest = selected[-1]
        events: list[Event] = []
        facts: list[TrajectoryFact] = []
        for message_events, message_facts, coordinate in reversed(selected_output):
            events.extend(replace(event, raw_index=coordinate) for event in message_events)
            facts.extend(message_facts)
        newest_cursor = self._encode_history_cursor(sid, identity, newest)
        older_cursor = self._encode_history_cursor(sid, identity, oldest) if has_more else None
        return HistoryPage(
            location=f"opencode://{sid}",
            events=events,
            trajectory=facts,
            trajectory_events=(),
            cursor=newest_cursor,
            older_cursor=older_cursor,
            has_older=has_more,
            provenance=self._attachment_provenance(sid),
            pinned=pinned,
        )

    def _history_session(self, conn: sqlite3.Connection) -> str | None:
        if self._session is not None:
            return self._session if self._session_exists(conn, self._session) else None
        if self._receipt is not None:
            sid = self._read_receipt()
            if sid is not None and self._session_exists(conn, sid):
                return sid
        pinned = self._pinned_sid()
        if pinned is not None and self._trusted_known_location():
            return pinned if self._session_exists(conn, pinned) else None
        if self._session_id is not None and self._session_exists(conn, self._session_id):
            return self._session_id
        if not self._cwd:
            return None
        row = session_for_history(conn, str(Path(self._cwd).resolve()), self._after)
        return str(row[0]) if row is not None else None

    @staticmethod
    def _history_row_key(created, message_id) -> tuple[int | float, str, str] | None:
        if isinstance(created, bool) or not isinstance(created, (int, float)):
            return None
        if not math.isfinite(created):
            return None
        if not isinstance(message_id, str) or not message_id:
            return None
        return created, message_id, ""

    @staticmethod
    def _history_revision(updated, created) -> int:
        value = (
            updated
            if isinstance(updated, (int, float)) and not isinstance(updated, bool)
            else created
        )
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            return 0
        return max(0, int(value))

    @classmethod
    def _history_coordinate(cls, created) -> int:
        return cls._history_revision(created, 0)

    @classmethod
    def _stored_revision(cls, data: dict, updated, created) -> int:
        persisted = cls._history_revision(updated, created)
        revision = data.get("revision")
        if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0:
            return max(revision, persisted)
        return persisted

    @staticmethod
    def _history_fingerprint(updated, raw) -> str:
        if isinstance(raw, bytes):
            encoded = raw
        elif isinstance(raw, str):
            encoded = raw.encode("utf-8", errors="replace")
        else:
            encoded = repr(raw).encode("utf-8", errors="replace")
        return hashlib.sha256(str(updated).encode("utf-8") + b"\0" + encoded).hexdigest()

    def _encode_history_cursor(
        self, sid: str, identity: dict[str, int], row: Sequence[object]
    ) -> str:
        message_id, created, updated, raw = row
        key = self._history_row_key(created, message_id)
        if key is None:
            raise ValueError("cannot encode an invalid OpenCode history boundary")
        payload = {
            "v": 1,
            "source": "opencode",
            "db": _opencode_source_key(self._db),
            "session": sid,
            "identity": identity,
            "boundary": [key[0], key[1]],
            "revision": self._history_revision(updated, created),
            "fingerprint": self._history_fingerprint(updated, raw),
        }
        encoded = (
            base64.urlsafe_b64encode(
                json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
            )
            .decode("ascii")
            .rstrip("=")
        )
        cursor = "oc1." + encoded
        if len(cursor.encode("utf-8")) > TRAJECTORY_CURSOR_MAX_BYTES:
            raise ValueError("OpenCode history cursor exceeds its size limit")
        return cursor

    @staticmethod
    def _decode_history_cursor(cursor: str) -> dict[str, object]:
        if not isinstance(cursor, str) or not cursor.startswith("oc1."):
            raise _OpenCodeHistoryPageError(
                "history_cursor_invalid", "history cursor is not valid for OpenCode"
            )
        try:
            encoded_length = len(cursor.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise _OpenCodeHistoryPageError(
                "history_cursor_invalid", "history cursor is malformed"
            ) from exc
        if encoded_length > TRAJECTORY_CURSOR_MAX_BYTES:
            raise _OpenCodeHistoryPageError("history_cursor_invalid", "history cursor is too large")
        try:
            raw = base64.urlsafe_b64decode(cursor[4:] + "=" * (-len(cursor[4:]) % 4))
            payload = json.loads(raw.decode("utf-8"))
        except _OpenCodeHistoryPageError:
            raise
        except (ValueError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError) as exc:
            raise _OpenCodeHistoryPageError(
                "history_cursor_invalid", "history cursor is malformed"
            ) from exc
        if not isinstance(payload, dict):
            raise _OpenCodeHistoryPageError(
                "history_cursor_invalid", "history cursor payload is malformed"
            )
        required = {
            "v",
            "source",
            "db",
            "session",
            "identity",
            "boundary",
            "revision",
            "fingerprint",
        }
        if set(payload) != required or payload.get("v") != 1 or payload.get("source") != "opencode":
            raise _OpenCodeHistoryPageError(
                "history_cursor_invalid", "history cursor does not belong to OpenCode"
            )
        return payload

    def _validate_history_cursor(
        self,
        conn: sqlite3.Connection,
        cursor: dict[str, object],
        sid: str,
        identity: dict[str, int],
    ) -> tuple[int | float, str, str]:
        if cursor.get("db") != _opencode_source_key(self._db) or cursor.get("session") != sid:
            raise _OpenCodeHistoryPageError(
                "history_cursor_invalid", "history cursor belongs to another source or session"
            )
        found_identity = cursor.get("identity")
        valid_identity = False
        if isinstance(found_identity, dict):
            found_size = found_identity.get("size")
            valid_identity = (
                found_identity.get("dev") == identity["dev"]
                and found_identity.get("ino") == identity["ino"]
                and type(found_size) is int
            )
        if not valid_identity:
            raise _OpenCodeHistoryPageError(
                "history_cursor_invalid", "OpenCode database identity changed"
            )
        boundary = cursor.get("boundary")
        if (
            not isinstance(boundary, list)
            or len(boundary) != 2
            or isinstance(boundary[0], bool)
            or not isinstance(boundary[0], (int, float))
            or not math.isfinite(boundary[0])
            or not isinstance(boundary[1], str)
            or not boundary[1]
        ):
            raise _OpenCodeHistoryPageError(
                "history_cursor_invalid", "history cursor boundary is malformed"
            )
        created, message_id = boundary
        row = history_boundary(conn, sid, created, message_id)
        if row is None:
            raise _OpenCodeHistoryPageError(
                "history_cursor_invalid", "OpenCode history boundary no longer exists"
            )
        if cursor.get("revision") != self._history_revision(row[0], created):
            raise _OpenCodeHistoryPageError(
                "history_cursor_invalid", "OpenCode history boundary was updated"
            )
        if cursor.get("fingerprint") != self._history_fingerprint(row[0], row[1]):
            raise _OpenCodeHistoryPageError(
                "history_cursor_invalid", "OpenCode history boundary was updated"
            )
        return created, message_id, str(cursor["fingerprint"])

    def _history_parts(
        self, conn: sqlite3.Connection, sid: str, message_id: str, limit: int
    ) -> tuple[list[tuple[object, object, object, object]], bool]:
        found = paged_parts(conn, sid, message_id, limit)
        truncated = len(found) > limit
        found = found[:limit]
        found.sort(
            key=lambda row: (
                row[1] if isinstance(row[1], (int, float)) and not isinstance(row[1], bool) else 0,
                str(row[0]),
            )
        )
        return found, truncated
