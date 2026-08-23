"""TranscriptSource: tail an append-only transcript file.

The implementation of the file-backed ``Source``. Holds the byte offset, record
index and mtime that used to live on the observer's cursor. Nothing above it
knows the input is a file.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import errno
import hashlib
import json
import logging
import os
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, cast

from theater.constants.trajectory import (
    TRAJECTORY_CURSOR_MAX_BYTES,
    TRAJECTORY_PAGE_RECORD_LIMIT,
    TRAJECTORY_TRANSCRIPT_CURSOR_FINGERPRINT_BYTES,
    TRAJECTORY_TRANSCRIPT_HISTORY_MAX_SCAN_BYTES,
    TRAJECTORY_TRANSCRIPT_HISTORY_WINDOW_BYTES,
)
from theater.harness.contracts.events import Event
from theater.harness.contracts.source import (
    Attachment,
    Batch,
    History,
    HistoryPage,
    IdentityLossEvidence,
    ReceiptAdmission,
    Source,
    SourceContractError,
    StreamPoint,
    bound_history_event,
)
from theater.harness.contracts.trajectory import ParsedRecord, TrajectoryFact
from theater.harness.transcript.attachment import attach_point
from theater.provenance import (
    TranscriptProvenance,
    is_trusted_provenance,
    normalize_provenance,
)
from theater.transcript_identity import (
    TRANSCRIPT_IDENTITY_LOST_CODE,
    TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
    trusted_location_unavailable_reason,
)

if TYPE_CHECKING:
    from theater.harness.transcript.observer import TranscriptObserver

logger = logging.getLogger("theater.harness.source")


def _bounded_history_event(event: Event) -> Event:
    return bound_history_event(event)


class _HistoryPageError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TranscriptSource(Source):
    """Tail an append-only transcript file. What ``TranscriptObserver`` returns.

    Holds the byte offset, record index and mtime that used to live on the
    observer's cursor. Nothing above it knows the input is a file.
    """

    def __init__(
        self,
        observer: TranscriptObserver,
        *,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
        allow_refresh: bool = False,
        exact_attachments: bool = False,
        session_provenance: str | TranscriptProvenance | None = None,
        collision_domain: str | None = None,
        known_location: str | None = None,
    ) -> None:
        self._observer = observer
        self._cwd = cwd
        #: Updated when attach reveals the harness's own session id for re-attach.
        self._session_id = session_id
        self._after = after
        self._allow_refresh = allow_refresh
        self._exact_attachments = exact_attachments
        self._session_provenance = normalize_provenance(session_provenance)
        self.collision_domain = collision_domain
        self._domain_root = Path(collision_domain).resolve() if collision_domain else None
        self._known_location = Path(known_location) if known_location else None
        self._known_location_provenance = (
            self._session_provenance
            if self._known_location is not None
            else TranscriptProvenance.HEURISTIC
        )
        #: Proven locations with strength; held here so proof and trust agree.
        self._proven: dict[Path, TranscriptProvenance] = {}
        self.path: Path | None = None
        self.offset = 0
        self.index = 0
        self.mtime = 0
        self._pending: tuple[Path, int, int, int, str | None] | None = None
        #: Trusted pin must be absent twice before ENOENT becomes identity loss.
        self._missing_trusted_pin_once: Path | None = None

    async def read(self) -> Batch:
        self._require_decision()
        if self.path is None:
            if reason := self._trusted_known_location_unavailable_reason():
                assert self._known_location is not None
                return self._confirmed_missing_pin_batch(self._known_location, reason)
            try:
                attached = await self._attach()
            except OSError as exc:
                if self._known_location_is_trusted() and exc.errno == errno.ENOENT:
                    assert self._known_location is not None
                    return self._confirmed_missing_pin_batch(
                        self._known_location,
                        f"trusted transcript pin {str(self._known_location)!r} "
                        "no longer exists on disk",
                    )
                self._missing_trusted_pin_once = None
                return self._source_unavailable_batch(exc)
            self._missing_trusted_pin_once = None
            return Batch(attached=attached) if attached else Batch(waiting=True)
        try:
            batch = self._drain()
        except OSError as exc:
            if self._path_is_trusted_pin(self.path) and exc.errno == errno.ENOENT:
                return self._confirmed_missing_pin_batch(
                    self.path,
                    f"trusted transcript pin {str(self.path)!r} no longer exists on disk",
                )
            if exc.errno == errno.ENOENT:
                # Heuristic transcript deleted or rotated; drop back to searching.
                self._missing_trusted_pin_once = None
                self._known_location = None
                self._detach()
                return Batch(waiting=True)
            self._missing_trusted_pin_once = None
            return self._source_unavailable_batch(exc)
        else:
            self._missing_trusted_pin_once = None
            return batch

    async def refresh(self) -> Batch:
        """Propose the newest transcript if the harness started a new one.

        Located by cwd alone, ignoring the session id: vibe opens a new session
        directory every turn, and the id we stored pins `find_transcript` to
        the first one, which never grows again.

        The same path back means the agent is idle rather than rotated, and
        returns an empty batch so the screen and rescue timers keep counting.
        The relocate arm may throttle its own clock, but must not reset either
        of the other clocks.
        """
        self._require_decision()
        path = await self._proven_rotation()
        if path is None and self._allow_refresh:
            path = await self._locate(session_id=None)
        if path is None or path == self.path:
            return Batch()
        session_id = self._observer.session_id(path)
        if self._trusted_pin_is_being_replaced_by_guess(path, session_id):
            # Heuristic movement is never an attachment; probe reports it only.
            return Batch()
        logger.info("transcript rotated: %s -> %s", self.path, path)
        attached = await self._attach(path)
        return Batch(attached=attached) if attached else Batch()

    async def probe_identity_loss(self) -> IdentityLossEvidence | None:
        """Look for a newer heuristic candidate without staging it.

        The adapter owns the bounded search. This source supplies the accepted
        path and cursor mtime, then rejects trusted results: exact/proven
        rotations belong to :meth:`refresh`, while this channel exists only to
        support quarantine evidence.
        """
        if self.path is None or not self._path_is_trusted_pin(self.path):
            return None
        candidate = await asyncio.to_thread(
            self._observer.identity_loss_candidate,
            cwd=self._cwd,
            current=self.path,
            current_mtime_ns=self.mtime,
            after=self._after,
        )
        if candidate is None or candidate == self.path or not self._inside_domain(candidate):
            return None
        session_id = self._observer.session_id(candidate)
        if is_trusted_provenance(self.correlation_for(candidate, session_id)):
            return None
        return IdentityLossEvidence(location=str(candidate), session_id=session_id)

    def commit_attachment(self) -> None:
        """Make the observer-accepted candidate the live transcript."""
        if self._pending is None:
            raise RuntimeError("no transcript attachment is pending")
        path, offset, index, mtime, session_id = self._pending
        provenance = normalize_provenance(self.correlation_for(path, session_id))
        self.path, self.offset, self.index, self.mtime = path, offset, index, mtime
        if session_id:
            if provenance is not TranscriptProvenance.EXACT:
                # The id was read off a guessed file; an exact claim would launder it.
                self._session_provenance = TranscriptProvenance.HEURISTIC
            self._session_id = session_id
        self._known_location = path
        self._known_location_provenance = provenance
        self._pending = None

    def discard_attachment(self) -> None:
        """Reject a candidate while continuing to watch the accepted file."""
        if self._pending is None:
            raise RuntimeError("no transcript attachment is pending")
        self._pending = None

    def revoke_attachment(self) -> None:
        """Return to discovery after an exact claimant proves this was wrong."""
        self._pending = None
        self._detach()
        # The id came from the revoked file; retaining it would re-select it.
        self._session_id = None
        self._known_location = None
        self._known_location_provenance = TranscriptProvenance.HEURISTIC

    def admit_exact_location(self, *, location: str, session_id: str) -> ReceiptAdmission:
        """Use a receipt-proven path on the next read."""
        path = Path(location)
        self._pending = None
        self._session_id = session_id
        self._session_provenance = TranscriptProvenance.EXACT
        self._known_location = path
        self._known_location_provenance = TranscriptProvenance.EXACT
        self._proven[path] = TranscriptProvenance.EXACT
        if self.path == path:
            return "accepted"
        self._detach()
        return "staged"

    async def history(self, *, last_n: int) -> History:
        """Re-read the whole transcript with the text left unclipped.

        Located from scratch rather than reusing `self.path`, so this works on
        a source that has never polled — which is the normal case, since the
        caller opens one just for this.
        """
        pinned = self._known_location is not None
        path = self.path
        if path is None and self._known_location is not None:
            if reason := self._trusted_known_location_unavailable_reason():
                return History(
                    error_code=TRANSCRIPT_IDENTITY_LOST_CODE,
                    error=reason,
                    pinned=True,
                )
            path = await self._upgraded(self._known_location)
        if path is None:
            path = await self._locate(session_id=self._session_id)
        if path is None:
            return History(pinned=pinned)
        if path_error := self._history_path_error(path, pinned=pinned):
            return path_error
        try:
            events = await asyncio.to_thread(
                self._read_all,
                path,
                strict=self._path_is_trusted_pin(path),
            )
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                return History(
                    error_code=TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
                    error=f"transcript source {str(path)!r} is unavailable: {exc}",
                    pinned=True,
                )
            return History(
                error_code=TRANSCRIPT_IDENTITY_LOST_CODE,
                error=f"trusted transcript pin {str(path)!r} no longer exists on disk",
                pinned=True,
            )
        events = [event for event in events if not event.usage_only]
        return History(
            location=str(path),
            events=events[-last_n:] if last_n > 0 else events,
            correlation=self.correlation_for(path, self._observer.session_id(path)),
            collision_domain=self.collision_domain,
            pinned=pinned,
        )

    async def history_page(
        self, *, before: str | None = None, limit: int = TRAJECTORY_PAGE_RECORD_LIMIT
    ) -> HistoryPage:
        """Read a bounded JSONL window without touching the live tail cursor."""
        if type(limit) is not int or limit <= 0:
            return HistoryPage(
                error_code="invalid_limit", error="history page limit must be positive"
            )
        limit = min(limit, TRAJECTORY_PAGE_RECORD_LIMIT)
        pinned = self._known_location is not None
        path = self.path
        if path is None and self._known_location is not None:
            if reason := self._trusted_known_location_unavailable_reason():
                return HistoryPage(
                    error_code=TRANSCRIPT_IDENTITY_LOST_CODE, error=reason, pinned=True
                )
            path = await self._upgraded(self._known_location)
        if path is None:
            path = await self._locate(session_id=self._session_id)
        if path is None:
            return HistoryPage(pinned=pinned)
        if path_error := self._history_path_error(path, pinned=pinned):
            if before is not None and path_error.error_code is None:
                return HistoryPage(
                    location=path_error.location,
                    error_code="history_cursor_invalid",
                    error="history cursor cannot be used because the transcript is unavailable",
                    provenance=path_error.correlation,
                    pinned=path_error.pinned,
                )
            return HistoryPage(
                location=path_error.location,
                error_code=path_error.error_code,
                error=path_error.error,
                provenance=path_error.correlation,
                pinned=path_error.pinned,
            )
        try:
            end, end_index, cursor_identity = (
                self._decode_page_cursor(before, path) if before is not None else (None, None, None)
            )
            live_offset = self.offset if before is None and self.path == path else None
            live_index = self.index if before is None and self.path == path else None
            (
                events,
                facts,
                start,
                page_end,
                start_index,
                page_end_index,
                identity,
                older_identity,
            ) = await asyncio.to_thread(
                self._read_page,
                path,
                end=end,
                end_index=end_index if before is not None else live_index,
                live_offset=live_offset,
                limit=limit,
                expected_identity=cursor_identity,
            )
        except _HistoryPageError as exc:
            return HistoryPage(error_code=exc.code, error=str(exc), pinned=pinned)
        except ValueError as exc:
            return HistoryPage(error_code="history_cursor_invalid", error=str(exc), pinned=pinned)
        except OSError as exc:
            if exc.errno == errno.ENOENT and self._path_is_trusted_pin(path):
                return HistoryPage(
                    error_code=TRANSCRIPT_IDENTITY_LOST_CODE,
                    error=f"trusted transcript pin {str(path)!r} no longer exists on disk",
                    pinned=True,
                )
            return HistoryPage(
                error_code=TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
                error=f"transcript source {str(path)!r} is unavailable: {exc}",
                pinned=pinned,
            )
        session_id = self._observer.session_id(path)
        return HistoryPage(
            location=str(path),
            events=events,
            trajectory=facts,
            cursor=self._encode_page_cursor(path, page_end, page_end_index, identity),
            older_cursor=(
                self._encode_page_cursor(path, start, start_index, older_identity)
                if 0 < start < page_end
                else None
            ),
            has_older=0 < start < page_end,
            provenance=self.correlation_for(path, session_id),
            pinned=pinned,
        )

    def _history_path_error(self, path: Path, *, pinned: bool) -> History | None:
        """Validate a history location without turning generic I/O into loss."""
        try:
            inside_domain = self._inside_domain(path)
        except OSError as exc:
            return History(
                error_code=TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
                error=f"transcript source {str(path)!r} is unavailable: {exc}",
                pinned=pinned,
            )
        if not inside_domain:
            if self._path_is_trusted_pin(path):
                return History(
                    error_code=TRANSCRIPT_IDENTITY_LOST_CODE,
                    error=(
                        f"trusted transcript pin {str(path)!r} no longer exists inside its "
                        "trusted transcript domain"
                    ),
                    pinned=True,
                )
            return History(pinned=pinned)
        if pinned:
            try:
                path.stat()
            except OSError as exc:
                # Never replace an admitted historical location with a cwd guess.
                if self._path_is_trusted_pin(path) and exc.errno == errno.ENOENT:
                    return History(
                        error_code=TRANSCRIPT_IDENTITY_LOST_CODE,
                        error=f"trusted transcript pin {str(path)!r} no longer exists on disk",
                        pinned=True,
                    )
                if exc.errno != errno.ENOENT:
                    return History(
                        error_code=TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
                        error=f"transcript source {str(path)!r} is unavailable: {exc}",
                        pinned=True,
                    )
                return History(pinned=True)
        return None

    def correlation_for(self, path: Path, session_id: str | None) -> str:
        """How well *path* is known to belong to this participant.

        A method, and the one place both `read` and `history` ask the
        question, because exactness is a property of the **location** rather
        than of the source. A subclass whose discovery sometimes proves
        ownership — and sometimes falls back to the same cwd scan as everyone
        else — cannot answer with a flag fixed at construction without
        claiming proof for the fallback.

        Three ways to be trusted are known here. ``exact_attachments`` says
        every candidate under this source's root has one possible owner by
        construction (a participant-isolated save directory). A location the
        observer's proof channel answered with is proven by definition, and is
        recorded here rather than trusted to the adapter. Exact session
        provenance says the id we were given was itself exact — a launch
        receipt, or an earlier exact proof already persisted — so a file
        carrying that id is the right one. Persisted proven/operator
        provenance is narrower: it trusts only the persisted known location.
        """
        if self._exact_attachments and self._inside_domain(path):
            return str(TranscriptProvenance.EXACT)
        if path in self._proven:
            return str(self._proven[path])
        if (
            self._known_location is not None
            and path == self._known_location
            and is_trusted_provenance(self._known_location_provenance)
        ):
            return str(self._known_location_provenance)
        if (
            self._session_provenance is TranscriptProvenance.EXACT
            and self._session_id is not None
            and session_id is not None
            and session_id == self._session_id
        ):
            return str(TranscriptProvenance.EXACT)
        return str(TranscriptProvenance.HEURISTIC)

    def _read_all(self, path: Path, *, strict: bool = False) -> list[Event]:
        events: list[Event] = []
        try:
            with path.open("rb") as fh:
                offset = 0
                for index, raw in enumerate(fh):
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line:
                        parsed = self._parse_record(line, index, clip_text=False)
                        decorated = self._decorate_parsed(parsed, offset)
                        events.extend(decorated.events)
                    offset += len(raw)
        except OSError:
            if strict:
                raise
            # A transcript that vanished mid-read is the same non-event here.
            return []
        return events

    def _read_page(
        self,
        path: Path,
        *,
        end: int | None,
        end_index: int | None,
        live_offset: int | None,
        limit: int,
        expected_identity: dict[str, object] | None,
    ) -> tuple[
        list[Event],
        list[TrajectoryFact],
        int,
        int,
        int | None,
        int | None,
        dict[str, object],
        dict[str, object],
    ]:
        with path.open("rb") as fh:
            stat = os.fstat(fh.fileno())
            total = int(stat.st_size)
            if expected_identity is not None:
                self._validate_page_identity(fh, stat, expected_identity)
                snapshot_size = cast(int, expected_identity["size"])
            else:
                snapshot_size = total
            page_end = total if end is None else end
            if page_end < 0 or page_end > snapshot_size or page_end > total:
                raise ValueError("history cursor is outside the transcript")
            base_identity = (
                dict(expected_identity)
                if expected_identity is not None
                else self._page_file_identity(fh, stat, boundary_offset=page_end)
            )
            page_end_index = end_index
            if end is None and live_offset != total:
                page_end_index = None
            scan_start, data = self._read_reverse_window(fh, page_end)
            at_boundary = scan_start == 0 or self._byte_at(fh, scan_start - 1) == b"\n"
            if scan_start and not at_boundary:
                first_newline = data.find(b"\n")
                if first_newline < 0:
                    raise _HistoryPageError(
                        "history_record_too_large",
                        "history page cannot bound a record within the reverse scan limit",
                    )
                data = data[first_newline + 1 :]
                scan_start += first_newline + 1
            lines: list[tuple[int, bytes]] = []
            offset = scan_start
            for raw in data.splitlines(keepends=True):
                if not raw.endswith(b"\n"):
                    break
                lines.append((offset, raw[:-1]))
                offset += len(raw)
            selected = lines[-limit:]
            selected_position = len(lines) - len(selected)
            if page_end_index is None:
                page_start_index = selected_position
                selected_end_index: int | None = None
            else:
                page_start_index = page_end_index - len(lines) + selected_position
                selected_end_index = page_end_index
            events: list[Event] = []
            facts: list[TrajectoryFact] = []
            for position, (record_offset, raw) in enumerate(selected):
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                parsed = self._parse_record(line, page_start_index + position, clip_text=False)
                decorated = self._decorate_parsed(parsed, record_offset)
                events.extend(
                    _bounded_history_event(event)
                    for event in decorated.events
                    if not event.usage_only
                )
                facts.extend(decorated.trajectory)
            start = selected[0][0] if selected else page_end
            start_index = page_start_index if page_end_index is not None else None
            identity = self._identity_at_boundary(fh, base_identity, page_end)
            older_identity = self._identity_at_boundary(fh, base_identity, start)
            return (
                events[:limit],
                facts[:limit],
                start,
                page_end,
                start_index,
                selected_end_index,
                identity,
                older_identity,
            )

    def _parse_record(self, line: str, index: int, *, clip_text: bool) -> ParsedRecord:
        missing = object()
        parser = getattr(self._observer, "parse_record", missing)
        if parser is missing:
            return ParsedRecord(
                events=tuple(self._observer.parse(line, index, clip_text=clip_text))
            )
        if not callable(parser):
            raise SourceContractError("TranscriptObserver.parse_record must be callable")
        parsed = parser(line, index, clip_text=clip_text)
        if not isinstance(parsed, ParsedRecord):
            raise SourceContractError("TranscriptObserver.parse_record must return ParsedRecord")
        return parsed

    @staticmethod
    def _decorate_parsed(parsed: ParsedRecord, source_offset: int) -> ParsedRecord:
        return ParsedRecord(
            events=tuple(replace(event, source_offset=source_offset) for event in parsed.events),
            trajectory=tuple(
                replace(fact, source_offset=source_offset) for fact in parsed.trajectory
            ),
        )

    @staticmethod
    def _byte_at(fh: BinaryIO, offset: int) -> bytes:
        fh.seek(offset)
        return fh.read(1)

    @classmethod
    def _read_reverse_window(cls, fh: BinaryIO, page_end: int) -> tuple[int, bytes]:
        if page_end <= 0:
            return 0, b""
        minimum = max(0, page_end - TRAJECTORY_TRANSCRIPT_HISTORY_MAX_SCAN_BYTES)
        scan_start = max(0, page_end - TRAJECTORY_TRANSCRIPT_HISTORY_WINDOW_BYTES)
        fh.seek(scan_start)
        data = fh.read(page_end - scan_start)
        while scan_start > minimum:
            at_boundary = scan_start == 0 or cls._byte_at(fh, scan_start - 1) == b"\n"
            first_newline = data.find(b"\n")
            if at_boundary or (first_newline >= 0 and first_newline < len(data) - 1):
                break
            new_start = max(minimum, scan_start - TRAJECTORY_TRANSCRIPT_HISTORY_WINDOW_BYTES)
            fh.seek(new_start)
            prefix = fh.read(scan_start - new_start)
            data = prefix + data
            scan_start = new_start
        at_boundary = scan_start == 0 or cls._byte_at(fh, scan_start - 1) == b"\n"
        first_newline = data.find(b"\n")
        if not at_boundary and (first_newline < 0 or first_newline == len(data) - 1):
            raise _HistoryPageError(
                "history_record_too_large",
                "history page cannot bound a record within the reverse scan limit",
            )
        return scan_start, data

    @staticmethod
    def _page_path_key(path: Path) -> str:
        return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:24]

    @classmethod
    def _encode_page_cursor(
        cls,
        path: Path,
        end: int,
        end_index: int | None,
        identity: dict[str, object],
    ) -> str:
        payload = {
            "v": 3,
            "path": cls._page_path_key(path),
            "identity": identity,
            "end": end,
            "end_index": end_index,
        }
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return "trj1." + base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")

    @classmethod
    def _decode_page_cursor(
        cls, cursor: str | None, path: Path
    ) -> tuple[int, int | None, dict[str, object]]:
        if not isinstance(cursor, str) or not cursor.startswith("trj1."):
            raise ValueError("history cursor is not valid for a transcript source")
        try:
            if len(cursor.encode("utf-8")) > TRAJECTORY_CURSOR_MAX_BYTES:
                raise ValueError("history cursor is too large")
        except UnicodeEncodeError as exc:
            raise ValueError("history cursor is malformed") from exc
        try:
            raw = base64.urlsafe_b64decode(cursor[5:] + "=" * (-len(cursor[5:]) % 4))
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError) as exc:
            raise ValueError("history cursor is malformed") from exc
        if not isinstance(payload, dict):
            raise ValueError(  # noqa: TRY004
                "history cursor does not belong to this transcript source"
            )
        end = payload.get("end")
        end_index = payload.get("end_index")
        if (
            set(payload) != {"v", "path", "identity", "end", "end_index"}
            or payload.get("v") != 3
            or payload.get("path") != cls._page_path_key(path)
            or type(end) is not int
            or end < 0
            or (end_index is not None and type(end_index) is not int)
            or (isinstance(end_index, int) and end_index < 0)
            or not isinstance(payload.get("identity"), dict)
        ):
            raise ValueError("history cursor does not belong to this transcript source")
        identity = cast(dict[str, object], payload["identity"])
        return end, cast(int | None, end_index), identity

    @classmethod
    def _page_file_identity(
        cls,
        fh: BinaryIO,
        stat: os.stat_result,
        *,
        snapshot_size: int | None = None,
        boundary_offset: int | None = None,
    ) -> dict[str, object]:
        sample_size = TRAJECTORY_TRANSCRIPT_CURSOR_FINGERPRINT_BYTES
        size = int(stat.st_size) if snapshot_size is None else snapshot_size
        boundary = size if boundary_offset is None else boundary_offset
        if boundary < 0 or boundary > size:
            raise ValueError("history cursor boundary is outside the transcript")
        fh.seek(0)
        head = fh.read(min(sample_size, size))
        boundary_start = max(0, boundary - sample_size)
        fh.seek(boundary_start)
        boundary_bytes = fh.read(boundary - boundary_start)
        return {
            "dev": int(stat.st_dev),
            "ino": int(stat.st_ino),
            "size": size,
            "mtime_ns": int(stat.st_mtime_ns),
            "ctime_ns": int(stat.st_ctime_ns),
            "head": base64.urlsafe_b64encode(head).decode("ascii"),
            "boundary_offset": boundary,
            "boundary": base64.urlsafe_b64encode(boundary_bytes).decode("ascii"),
        }

    @classmethod
    def _identity_at_boundary(
        cls, fh: BinaryIO, base: dict[str, object], boundary_offset: int
    ) -> dict[str, object]:
        snapshot_size = base["size"]
        if type(snapshot_size) is not int or boundary_offset < 0 or boundary_offset > snapshot_size:
            raise ValueError("history cursor boundary is outside the transcript")
        start = max(0, boundary_offset - TRAJECTORY_TRANSCRIPT_CURSOR_FINGERPRINT_BYTES)
        fh.seek(start)
        boundary = fh.read(boundary_offset - start)
        identity = dict(base)
        identity["boundary_offset"] = boundary_offset
        identity["boundary"] = base64.urlsafe_b64encode(boundary).decode("ascii")
        return identity

    @classmethod
    def _validate_page_identity(
        cls, fh: BinaryIO, stat: os.stat_result, expected: dict[str, object]
    ) -> None:
        required = {
            "dev",
            "ino",
            "size",
            "mtime_ns",
            "ctime_ns",
            "head",
            "boundary_offset",
            "boundary",
        }
        if set(expected) != required:
            raise ValueError("history cursor is invalid because its identity is malformed")
        dev = expected["dev"]
        ino = expected["ino"]
        snapshot_size = expected["size"]
        mtime_ns = expected["mtime_ns"]
        ctime_ns = expected["ctime_ns"]
        head_value = expected["head"]
        boundary_offset = expected["boundary_offset"]
        boundary_value = expected["boundary"]
        if not (
            type(dev) is int
            and type(ino) is int
            and type(snapshot_size) is int
            and snapshot_size >= 0
            and type(mtime_ns) is int
            and type(ctime_ns) is int
            and isinstance(head_value, str)
            and type(boundary_offset) is int
            and 0 <= boundary_offset <= snapshot_size
            and isinstance(boundary_value, str)
        ):
            raise ValueError("history cursor is invalid because its identity is malformed")
        try:
            head = base64.urlsafe_b64decode(head_value)
            boundary = base64.urlsafe_b64decode(boundary_value)
        except (ValueError, TypeError, binascii.Error) as exc:
            raise ValueError("history cursor is invalid because its identity is malformed") from exc
        if int(stat.st_dev) != dev or int(stat.st_ino) != ino:
            raise ValueError("history cursor is invalid because the transcript changed")
        if int(stat.st_size) < snapshot_size:
            raise ValueError("history cursor is invalid because the transcript shrank")
        if boundary_offset > int(stat.st_size):
            raise ValueError("history cursor is invalid because its boundary is unavailable")
        if int(stat.st_size) == snapshot_size and (
            int(stat.st_mtime_ns) != mtime_ns or int(stat.st_ctime_ns) != ctime_ns
        ):
            raise ValueError("history cursor is invalid because the transcript changed")
        current = cls._page_file_identity(
            fh,
            stat,
            snapshot_size=snapshot_size,
            boundary_offset=boundary_offset,
        )
        if current["head"] != expected["head"]:
            raise ValueError("history cursor is invalid because the transcript prefix changed")
        if current["boundary"] != expected["boundary"]:
            raise ValueError("history cursor is invalid because the transcript prefix changed")
        if (
            len(head) > TRAJECTORY_TRANSCRIPT_CURSOR_FINGERPRINT_BYTES
            or len(boundary) > TRAJECTORY_TRANSCRIPT_CURSOR_FINGERPRINT_BYTES
        ):
            raise ValueError("history cursor is invalid because its identity is malformed")

    # ---- internals ------------------------------------------------------

    def _known_location_is_trusted(self) -> bool:
        return self._known_location is not None and is_trusted_provenance(
            self._known_location_provenance
        )

    def _path_is_trusted_pin(self, path: Path | None) -> bool:
        return (
            path is not None
            and self._known_location is not None
            and path == self._known_location
            and is_trusted_provenance(self._known_location_provenance)
        )

    def _trusted_known_location_unavailable_reason(self) -> str | None:
        return trusted_location_unavailable_reason(
            location=str(self._known_location) if self._known_location is not None else None,
            provenance=str(self._known_location_provenance),
            domain=str(self._domain_root) if self._domain_root is not None else None,
        )

    @staticmethod
    def _identity_lost_batch(reason: str) -> Batch:
        return Batch(
            waiting=True,
            error_code=TRANSCRIPT_IDENTITY_LOST_CODE,
            error=reason,
        )

    @staticmethod
    def _source_unavailable_batch(exc: OSError) -> Batch:
        return Batch(
            waiting=True,
            error_code=TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
            error=f"transcript source is unavailable: {exc}",
        )

    def _confirmed_missing_pin_batch(self, path: Path, reason: str) -> Batch:
        """Require consecutive pin absence while its containing root is healthy."""
        root = self._domain_root or path.parent
        try:
            if not root.is_dir():
                raise NotADirectoryError(errno.ENOTDIR, "transcript root is unavailable", root)
        except OSError as exc:
            self._missing_trusted_pin_once = None
            return self._source_unavailable_batch(exc)
        if self._missing_trusted_pin_once == path:
            return self._identity_lost_batch(reason)
        self._missing_trusted_pin_once = path
        return Batch(waiting=True)

    def _trusted_pin_is_being_replaced_by_guess(self, path: Path, session_id: str | None) -> bool:
        return (
            self.path is not None
            and self._path_is_trusted_pin(self.path)
            and path != self.path
            and not is_trusted_provenance(self.correlation_for(path, session_id))
        )

    async def _proven_rotation(self) -> Path | None:
        """A process-proven replacement, if this adapter can supply one."""
        if self.path is None or not self._observer.proves_ownership:
            return None
        proven = await asyncio.to_thread(self._observer.proven_transcript, cwd=self._cwd)
        if proven is None or proven == self.path or not self._inside_domain(proven):
            return None
        self._proven[proven] = TranscriptProvenance.PROVEN
        return proven

    def _detach(self) -> None:
        """Forget an accepted file that vanished; collision rejection is staged."""
        self.path = None
        self.offset = self.index = self.mtime = 0

    def _require_decision(self) -> None:
        if self._pending is not None:
            raise RuntimeError("attachment must be committed or discarded before reading again")

    def _inside_domain(self, path: Path) -> bool:
        if self._domain_root is None:
            return True
        try:
            path.resolve().relative_to(self._domain_root)
        except ValueError:
            return False
        return True

    async def _locate(self, *, session_id: str | None) -> Path | None:
        if not self._cwd:
            return None
        path = await asyncio.to_thread(
            self._observer.find_transcript,
            cwd=self._cwd,
            session_id=session_id,
            after=self._after,
        )
        if path is not None and not self._inside_domain(path):
            logger.warning(
                "transcript candidate %s is outside source domain %s", path, self._domain_root
            )
            return None
        return path

    async def _upgraded(self, pinned: Path) -> Path:
        """*pinned*, unless the observer can prove a better location.

        A location admitted earlier is only as good as the evidence that
        admitted it. A heuristic one — the newest transcript in a shared
        working directory — may be a sibling's, and a participant that was
        bound that way stays bound that way forever: every later poll takes the
        pin before discovery is ever consulted, so proof that arrives
        afterwards never gets asked for. That is precisely the participant a
        proof channel is for, so a pin that is not already exact is offered to
        it once per attempt.

        Proof only, and deliberately not `find_transcript`: a probe that fails
        must leave the pin exactly as it was. Discovery would answer with a cwd
        guess instead, which is how an admitted location drifts onto a
        sibling's file — the one outcome worse than staying heuristic.
        """
        if not self._observer.proves_ownership:
            return pinned
        if (
            normalize_provenance(self.correlation_for(pinned, self._observer.session_id(pinned)))
            is not TranscriptProvenance.HEURISTIC
        ):
            return pinned
        proven = await asyncio.to_thread(self._observer.proven_transcript, cwd=self._cwd)
        if proven is None or not self._inside_domain(proven):
            return pinned
        self._proven[proven] = TranscriptProvenance.PROVEN
        if proven == pinned:
            return pinned
        logger.info(
            "transcript %s is held open by this participant's own process; "
            "replacing the location admitted from cwd evidence (%s)",
            proven,
            pinned,
        )
        return proven

    async def _attach(self, path: Path | None = None) -> Attachment | None:
        """Stage the end of a transcript. None if there is not one yet.

        Pass `path` to propose a known file (a rotation); omit it to go looking.
        """
        if path is None:
            path = self._known_location
            if path is not None and not self._inside_domain(path):
                path = None
            if path is not None:
                try:
                    path.stat()
                except OSError as exc:
                    if exc.errno != errno.ENOENT or self._path_is_trusted_pin(path):
                        raise
                    path = None
            if path is not None:
                path = await self._upgraded(path)
            if path is None:
                path = await self._locate(session_id=self._session_id)
            if path is None:
                return None
        if not self._inside_domain(path):
            return None
        size, lines, mtime, last_line, dev, ino = await asyncio.to_thread(attach_point, path)
        session_id = self._observer.session_id(path)
        last_event: Event | None = None
        if last_line is not None:
            parsed = self._parse_record(last_line, lines - 1, clip_text=True)
            semantic = [event for event in parsed.events if not event.usage_only]
            last_event = semantic[-1] if semantic else None
        self._pending = (path, size, lines, mtime, session_id)
        return Attachment(
            location=str(path),
            session_id=session_id,
            skipped=lines,
            last_event=last_event,
            point=StreamPoint(records=lines, size=size, dev=dev, ino=ino),
            correlation=self.correlation_for(path, session_id),
            collision_domain=self.collision_domain,
        )

    def _drain(self) -> Batch:
        """Read whatever the transcript grew by.

        Runs on the event loop rather than in a thread. It is a read of the
        bytes appended since the last poll — usually none, occasionally a few
        kilobytes — and the parse is pure, so the thread hop would cost more
        than it saves.
        """
        assert self.path is not None
        path, offset, index, mtime = self.path, self.offset, self.index, self.mtime

        st = path.stat()
        size = st.st_size
        # Same-length rewrite is indistinguishable from no-op; guessing wrong corrupts.
        if size < offset or (size == offset and st.st_mtime_ns != mtime):
            logger.info("transcript %s was rewritten; re-reading from the top", path)
            offset = index = 0
        if size == offset:
            self.mtime = st.st_mtime_ns
            return Batch()

        with path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read()
            mtime = os.fstat(fh.fileno()).st_mtime_ns
        head, sep, _tail = data.rpartition(b"\n")
        if not sep:
            # A record is still being written; partial JSON is not parseable.
            self.mtime = mtime
            return Batch()
        record_offset = offset
        offset += len(head) + 1

        events: list[Event] = []
        trajectory: list[TrajectoryFact] = []
        for raw in head.split(b"\n"):
            line = raw.decode("utf-8", errors="replace")
            parsed = self._parse_record(line, index, clip_text=True)
            decorated = self._decorate_parsed(parsed, record_offset)
            events.extend(decorated.events)
            trajectory.extend(decorated.trajectory)
            record_offset += len(raw) + 1
            index += 1

        progressed = offset != self.offset
        self.offset, self.index, self.mtime = offset, index, mtime
        return Batch(events=events, progressed=progressed, trajectory=trajectory)
