"""Bounded file tailing for Pi's append-only session JSONL."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from theater.harness.contracts.events import Event
from theater.harness.contracts.source import Attachment, Batch, StreamPoint
from theater.harness.contracts.trajectory import ParsedRecord, TrajectoryFact
from theater.harness.source import TranscriptSource
from theater.harness.transcript.discovery import stateful_history_reader
from theater.resume_floor import decode_floor

from .constants import PI_READ_BYTES, PI_RECORD_BYTES, PI_RECORDS_PER_BATCH

if TYPE_CHECKING:
    from .observer import PiObserver


def _attachment_point(path: Path) -> tuple[int, int, int, str | None, int | None, int | None]:
    """Read only bounded buffers while finding an attachment cursor and last record."""
    size = lines = 0
    tail = b""
    last_complete: bytes | None = None
    dropping = False
    with path.open("rb") as stream:
        while chunk := stream.read(PI_READ_BYTES):
            size += len(chunk)
            lines += chunk.count(b"\n")
            if dropping:
                newline = chunk.rfind(b"\n")
                if newline < 0:
                    continue
                last_complete = chunk[:newline].rsplit(b"\n", 1)[-1]
                tail = chunk[newline + 1 :]
                dropping = len(tail) > PI_RECORD_BYTES
                if dropping:
                    tail = b""
                continue
            combined = tail + chunk
            pieces = combined.split(b"\n")
            if len(pieces) > 1:
                last_complete = pieces[-2]
            tail = pieces[-1]
            if len(tail) > PI_RECORD_BYTES:
                tail = b""
                dropping = True
        stat = os.fstat(stream.fileno())
    last_line = (
        last_complete.decode("utf-8", errors="replace")
        if last_complete is not None and len(last_complete) <= PI_RECORD_BYTES
        else None
    )
    return size, lines, stat.st_mtime_ns, last_line, stat.st_dev, stat.st_ino


class PiTranscriptSource(TranscriptSource):
    """A TranscriptSource with bounded reads and oversized-record recovery."""

    if TYPE_CHECKING:
        _observer: PiObserver

    def __init__(self, *args, **kwargs) -> None:
        self._usage_floor = kwargs.pop("usage_floor", None)
        super().__init__(*args, **kwargs)
        self._backlog: list[tuple[bytes, int]] = []
        self._partial = b""
        self._partial_offset: int | None = None
        self._dropping_oversized = False
        #: Initial restart reconciliation parses only usage through this byte offset.
        self._usage_only_until: int | None = None

    def commit_attachment(self) -> None:
        super().commit_attachment()
        self._clear_live_buffers()
        self._seed_live_context()

    def _history_reader(self):
        from .observer import PiObserver

        return stateful_history_reader(
            clone=lambda: PiObserver(root=self._observer.root, isolated=self._observer.isolated),
            seed_of=lambda observer: observer._seed_history_context,
            decorate=self._decorate_parsed,
        )

    def discard_attachment(self) -> None:
        super().discard_attachment()
        self._seed_live_context()

    def revoke_attachment(self) -> None:
        super().revoke_attachment()
        self._usage_only_until = None
        self._clear_live_buffers()
        self._observer._reset_turn_context()

    def _detach(self) -> None:
        super()._detach()
        self._usage_only_until = None
        self._clear_live_buffers()
        self._observer._reset_turn_context()

    def _seed_live_context(self) -> None:
        if self.path is None:
            self._observer._reset_turn_context()
            return
        try:
            with self.path.open("rb") as stream:
                self._observer._seed_history_context(stream, self.offset)
        except OSError:
            self._observer._reset_turn_context()

    async def _attach(self, path: Path | None = None) -> Attachment | None:
        if path is None:
            path = self._known_location
            if path is not None and not self._inside_domain(path):
                path = None
            if path is None:
                path = await self._locate(session_id=self._session_id)
            if path is None:
                return None
        if not self._inside_domain(path):
            return None
        size, lines, mtime, last_line, dev, ino = await asyncio.to_thread(_attachment_point, path)
        session_id = self._observer.session_id(path)
        cursor = self._replay_cursor(size=size, lines=lines, dev=dev, ino=ino)
        offset, index = (size, lines) if cursor is None else cursor[:2]
        usage_only = cursor is not None and cursor[2]
        last_event: Event | None = None
        if cursor is None and last_line is not None:
            parsed = self._parse_record(last_line, max(0, lines - 1), clip_text=True)
            semantic = [event for event in parsed.events if not event.usage_only]
            last_event = semantic[-1] if semantic else None
        self._usage_only_until = size if usage_only else None
        self._pending = (path, offset, index, mtime, session_id)
        return Attachment(
            location=str(path),
            session_id=session_id,
            # A usage-only reconciliation intentionally skips all control
            # records even while it replays their accounting payloads.
            skipped=lines if usage_only else index,
            last_event=last_event,
            point=StreamPoint(records=lines, size=size, dev=dev, ino=ino),
            correlation=self.correlation_for(path, session_id),
            collision_domain=self.collision_domain,
        )

    def _replay_cursor(
        self,
        *,
        size: int,
        lines: int,
        dev: int | None,
        ino: int | None,
    ) -> tuple[int, int, bool] | None:
        """Return ``(offset, record_index, usage_only)`` for a safe replay.

        Pi sessions are participant-isolated, so a cold launch and a `/new`
        rotation may safely replay records written before the watcher attached.
        A resumed stream instead starts at its persisted pre-launch boundary.
        On daemon restart, only usage is replayed: control events have already
        been observed, but a stable usage key lets the durable ledger recover
        records written during downtime exactly once.
        """
        if not self._exact_attachments:
            return None
        if self.path is not None:
            # Pi's normal `/new` rotation creates a fresh session in this
            # isolated domain.  The candidate locator rejects pre-launch
            # sessions, so this path is safe to read from zero.
            return (0, 0, False)
        floor_cursor = self._floor_cursor(size=size, lines=lines, dev=dev, ino=ino)
        if floor_cursor is None:
            return None
        offset, index = floor_cursor
        if self._known_location is not None:
            return (offset, index, True)
        return (offset, index, False)

    def _floor_cursor(
        self,
        *,
        size: int,
        lines: int,
        dev: int | None,
        ino: int | None,
    ) -> tuple[int, int] | None:
        """Return the cold or validated resumed cursor; fail closed otherwise."""
        if self._usage_floor is None:
            return (0, 0)
        floor = decode_floor(self._usage_floor)
        if floor is None:
            return None
        floor_size, floor_records, floor_dev, floor_ino = (
            floor.size,
            floor.records,
            floor.dev,
            floor.ino,
        )
        if (
            floor_size is None
            or floor_records is None
            or floor_dev is None
            or floor_ino is None
            or dev is None
            or ino is None
        ):
            return None
        if floor_dev != dev or floor_ino != ino:
            return None
        if size < floor_size or lines < floor_records:
            return None
        return (floor_size, floor_records)

    def _drain(self) -> Batch:
        if self._backlog:
            return self._drain_records()
        assert self.path is not None
        path, offset, index, mtime = self.path, self.offset, self.index, self.mtime
        stat = path.stat()
        if stat.st_size < offset or (stat.st_size == offset and stat.st_mtime_ns != mtime):
            offset = index = 0
            self._usage_only_until = None
            self._clear_live_buffers()
            self._observer._reset_turn_context()
        if stat.st_size == offset:
            self.mtime = stat.st_mtime_ns
            return Batch()
        with path.open("rb") as stream:
            stream.seek(offset)
            data = stream.read(PI_READ_BYTES)
            read_stat = os.fstat(stream.fileno())
        if not data:
            self.mtime = read_stat.st_mtime_ns
            return Batch()
        self.offset = offset + len(data)
        self.mtime = read_stat.st_mtime_ns
        self.index = index
        malformed = self._accept_bytes(data, offset)
        batch = self._drain_records()
        batch = replace(batch, progressed=True)
        if malformed:
            return replace(
                batch,
                error_code="pi_transcript_oversized_record",
                error="ignored an oversized Pi transcript record",
            )
        return batch

    def _accept_bytes(self, data: bytes, offset: int) -> bool:
        malformed = False
        if self._dropping_oversized:
            newline = data.find(b"\n")
            if newline < 0:
                return True
            self._dropping_oversized = False
            self.index += 1
            data = data[newline + 1 :]
            offset += newline + 1
            malformed = True
        if self._partial:
            data = self._partial + data
            offset = self._partial_offset if self._partial_offset is not None else offset
            self._partial = b""
            self._partial_offset = None
        parts = data.split(b"\n")
        complete, partial = parts[:-1], parts[-1]
        record_offset = offset
        for raw in complete:
            if len(raw) > PI_RECORD_BYTES:
                malformed = True
                self.index += 1
            else:
                self._backlog.append((raw, record_offset))
            record_offset += len(raw) + 1
        if partial:
            if len(partial) > PI_RECORD_BYTES:
                self._dropping_oversized = True
                malformed = True
            else:
                self._partial = partial
                self._partial_offset = record_offset
        return malformed

    def _drain_records(self) -> Batch:
        records = self._backlog[:PI_RECORDS_PER_BATCH]
        del self._backlog[:PI_RECORDS_PER_BATCH]
        events: list[Event] = []
        trajectory: list[TrajectoryFact] = []
        trajectory_events: list[Event] = []
        for raw, source_offset in records:
            parsed: ParsedRecord = self._parse_record(
                raw.decode("utf-8", errors="replace"), self.index, clip_text=True
            )
            decorated = self._decorate_parsed(parsed, source_offset)
            if self._usage_only_until is not None and source_offset < self._usage_only_until:
                events.extend(self._usage_only(event) for event in decorated.events if event.usage)
            else:
                events.extend(decorated.events)
                trajectory.extend(decorated.trajectory)
                trajectory_events.extend(decorated.baseline_events)
            self.index += 1
        return Batch(
            events=events,
            progressed=bool(records),
            trajectory=trajectory,
            trajectory_events=trajectory_events,
        )

    def _clear_live_buffers(self) -> None:
        self._backlog.clear()
        self._partial = b""
        self._partial_offset = None
        self._dropping_oversized = False

    @staticmethod
    def _usage_only(event: Event) -> Event:
        """Keep restart accounting off the bus and out of turn completion."""
        assert event.usage is not None
        return Event(
            kind=event.kind,
            ts=event.ts,
            raw_index=event.raw_index,
            usage=event.usage,
            source_offset=event.source_offset,
        )
