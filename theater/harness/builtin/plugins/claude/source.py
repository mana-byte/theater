"""Claude transcript source and history setup."""

from __future__ import annotations

import asyncio
import errno
import os
from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from theater.constants.harness import HARNESS_TRANSCRIPT_SCAN_CHUNK_BYTES
from theater.harness.channels.filesystem import WatchGate
from theater.harness.contracts.events import clip
from theater.harness.normalization.timing import iso_epoch
from theater.harness.normalization.values import decode_json_record
from theater.harness.source import (
    Attachment,
    Batch,
    ReceiptAdmission,
    StreamPoint,
    TranscriptSource,
)
from theater.harness.transcript.history import HistoryParseContext
from theater.provenance import TranscriptProvenance

from .constants import SEAL_NODE_LIMIT

if TYPE_CHECKING:
    from .observer import ClaudeCodeObserver


def _open_claude_source(
    *,
    root: Path,
    relocate_by_cwd: bool,
    cwd: str | None,
    session_id: str | None = None,
    after: float | None = None,
    session_provenance: str | TranscriptProvenance | None = None,
    known_location: str | None = None,
) -> _ClaudeSource:
    from .observer import ClaudeCodeObserver

    return _ClaudeSource(
        ClaudeCodeObserver(root=root),
        cwd=cwd,
        session_id=session_id,
        after=after,
        allow_refresh=relocate_by_cwd,
        session_provenance=session_provenance,
        known_location=known_location,
    )


#: Short, because a relocated receipt file is only found by the real check.
_RECEIPT_FALLBACK_SECONDS = 2.0


def _check_attachment_snapshot(stat: os.stat_result, point: StreamPoint, mtime: int) -> None:
    if (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) != (
        point.dev,
        point.ino,
        point.size,
        mtime,
    ):
        raise OSError(errno.ESTALE, "transcript changed during attachment")


def _attachment_context(path: Path, point: StreamPoint, mtime: int) -> list[tuple[int, int, str]]:
    """Read bounded causal context from the scanned prefix, refusing a changed snapshot."""
    assert point.size is not None and point.records is not None
    chunks: deque[bytes] = deque()
    with path.open("rb") as handle:
        _check_attachment_snapshot(os.fstat(handle.fileno()), point, mtime)
        start = point.size
        newlines = 0
        # One extra newline proves the oldest retained record starts inside the snapshot.
        while start > 0 and newlines <= SEAL_NODE_LIMIT:
            length = min(start, HARNESS_TRANSCRIPT_SCAN_CHUNK_BYTES)
            start -= length
            handle.seek(start)
            raw = handle.read(length)
            if len(raw) != length:
                raise OSError(errno.ESTALE, "transcript changed during attachment")
            if not chunks and not raw.endswith(b"\n"):
                raise OSError(errno.ESTALE, "transcript attachment ends in an incomplete record")
            chunks.appendleft(raw)
            newlines += raw.count(b"\n")
        _check_attachment_snapshot(os.fstat(handle.fileno()), point, mtime)
    lines = b"".join(chunks).split(b"\n")[-SEAL_NODE_LIMIT - 1 : -1]
    offset = point.size - sum(len(line) + 1 for line in lines)
    records = []
    for index, raw in enumerate(lines, start=point.records - len(lines)):
        offset += len(raw) + 1
        records.append((index, offset, raw.decode("utf-8", errors="replace")))
    return records


def _live_turn_start(
    records: list[tuple[int, int, str]], after: float | None, session_id: str | None
) -> tuple[int, int] | None:
    """Locate a launch-local user turn assuming nondecreasing same-machine wall time."""
    # No predecessor floor is supplied; copied history cannot be fenced by record index.
    if after is None or session_id is None:
        return None
    previous = 0 if records and records[0][0] == 0 else None
    for index, offset, line in records:
        record = decode_json_record(line)
        if (
            previous is not None
            and record is not None
            and record.get("type") == "user"
            and record.get("isSidechain") is not True
            and record.get("sessionId") == session_id
            and (timestamp := iso_epoch(record.get("timestamp"))) is not None
            and timestamp > after
        ):
            return index, previous
        previous = offset
    return None


class _ClaudeSource(TranscriptSource):
    """Keep a receipt pending until its JSONL materializes.

    A receipt path is not identity loss until the file has existed.
    """

    if TYPE_CHECKING:
        _observer: ClaudeCodeObserver

    def __init__(self, observer: ClaudeCodeObserver, **kwargs) -> None:
        super().__init__(observer, **kwargs)
        # A restored pin is history; only an unbound launch may have an unread current turn.
        self._live_after = self._after if self._known_location is None else None
        self._expected_location: Path | None = None
        #: A receipt path is stat-polled only when its directory changed or the safety net is due.
        self._receipt_gate = WatchGate(fallback_seconds=_RECEIPT_FALLBACK_SECONDS)

    async def _attach(self, path: Path | None = None) -> Attachment | None:
        attached = await super()._attach(path)
        if attached is None:
            return None
        point = attached.point
        assert point is not None and self._pending is not None
        mtime = self._pending[3]
        try:
            records = await asyncio.to_thread(
                _attachment_context, Path(attached.location), point, mtime
            )
            self._observer.reset_stream()
            live_start = _live_turn_start(records, self._live_after, attached.session_id)
            offsets = {index: offset for index, offset, _ in records}
            last_event = None
            evidence_point = point
            for index, _, line in records:
                if live_start is not None and index == live_start[0]:
                    break
                for event in self._observer.parse(line, index, clip_text=False):
                    if not event.usage_only:
                        last_event = replace(event, text=clip(event.text))
                        evidence_point = replace(
                            point, records=event.raw_index + 1, size=offsets[event.raw_index]
                        )
                if index % 32 == 0:
                    await asyncio.sleep(0)
            stat = await asyncio.to_thread(Path(attached.location).stat)
            _check_attachment_snapshot(stat, point, mtime)
            if live_start is not None:
                pending_path, _, _, mtime, session_id = self._pending
                self._pending = (pending_path, live_start[1], live_start[0], mtime, session_id)
                # Copied history primes the seal; the current turn settles only through live reads.
                attached = replace(attached, status=None)
                last_event = None
        except (OSError, asyncio.CancelledError):
            self._pending = None
            self._observer.reset_stream()
            raise
        # Metadata after a boundary must not advance that boundary past a resume floor.
        return replace(attached, last_event=last_event, point=evidence_point)

    def commit_attachment(self) -> None:
        super().commit_attachment()
        self._live_after = None

    def bind_wakeup(self, wakeup) -> None:
        super().bind_wakeup(wakeup)
        self._receipt_gate.set_wake(wakeup)

    async def aclose(self) -> None:
        self._receipt_gate.release()
        await super().aclose()

    def _history_parse_context(self) -> HistoryParseContext:
        """History parses on a fresh clone so it never disturbs the live observer."""
        from .observer import ClaudeCodeObserver

        reader = ClaudeCodeObserver(root=self._observer.root)
        return HistoryParseContext(
            parse_record=lambda line, index: reader.parse_record(line, index, clip_text=False),
            prepare_history_parse=reader._seed_mcp_context,
        )

    async def read(self) -> Batch:
        self._require_decision()
        path = self._expected_location
        if path is None:
            self._receipt_gate.release()
            return await super().read()
        if not self._receipt_gate.due(path):
            return Batch(waiting=True)
        try:
            path.stat()
        except FileNotFoundError:
            relocated = await self._locate(session_id=self._session_id)
            if relocated is None or self._observer.session_id(relocated) != self._session_id:
                return Batch(waiting=True)
            path = relocated
        except OSError as exc:
            return self._source_unavailable_batch(exc)

        self._expected_location = None
        self._known_location = path
        self._known_location_provenance = TranscriptProvenance.EXACT
        self._proven[path] = TranscriptProvenance.EXACT
        return await super().read()

    def admit_exact_location(self, *, location: str, session_id: str) -> ReceiptAdmission:
        path = Path(location)
        self._pending = None
        self._session_id = session_id
        self._session_provenance = TranscriptProvenance.EXACT
        self._proven[path] = TranscriptProvenance.EXACT
        if self.path == path:
            self._expected_location = None
            self._known_location = path
            self._known_location_provenance = TranscriptProvenance.EXACT
            return "accepted"

        self._expected_location = path
        self._known_location = None
        self._known_location_provenance = TranscriptProvenance.HEURISTIC
        self._detach()
        return "staged"

    def revoke_attachment(self) -> None:
        self._expected_location = None
        super().revoke_attachment()
