"""Bounded file tailing for Pi's append-only session JSONL."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from theater.harness.contracts.events import Event
from theater.harness.contracts.source import Attachment, Batch, StreamPoint
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.harness.source import TranscriptSource
from theater.harness.transcript.discovery import stateful_history_reader
from theater.models import Status
from theater.resume_floor import decode_floor

from .constants import PI_READ_BYTES, PI_RECORD_BYTES, PI_RECORDS_PER_BATCH
from .parser import parse_live_records
from .record_buffer import PiRecord, RecordBuffer
from .record_projection import project_record

if TYPE_CHECKING:
    from .observer import PiObserver, PiSwitchBoundary


class _WaitForSwitch:
    pass


_WAIT_FOR_SWITCH = _WaitForSwitch()


def _checkpoint_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _encode_checkpoint(
    path: Path, offset: int, records: int, dev: int | None, ino: int | None
) -> str | None:
    if dev is None or ino is None:
        return None
    return json.dumps(
        {
            "version": 1,
            "location": str(path),
            "offset": offset,
            "records": records,
            "dev": dev,
            "ino": ino,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_checkpoint(raw: str | None) -> tuple[str, int, int, int, int] | None:
    if raw is None or len(raw) > 1024:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("version") != 1:
        return None
    location = value.get("location")
    if not isinstance(location, str) or not location:
        return None
    offset = _checkpoint_int(value.get("offset"))
    records = _checkpoint_int(value.get("records"))
    dev = _checkpoint_int(value.get("dev"))
    ino = _checkpoint_int(value.get("ino"))
    if offset is None or records is None or dev is None or ino is None:
        return None
    return location, offset, records, dev, ino


def _checkpoint_matches(
    checkpoint: tuple[str, int, int, int, int],
    path: Path,
    *,
    size: int,
    lines: int,
    dev: int | None,
    ino: int | None,
) -> bool:
    location, offset, records, checkpoint_dev, checkpoint_ino = checkpoint
    return (
        location == str(path)
        and dev == checkpoint_dev
        and ino == checkpoint_ino
        and size >= offset
        and lines >= records
    )


def _attachment_point(path: Path) -> tuple[int, int, int, str | None, int | None, int | None]:
    """Read only bounded buffers while finding an attachment cursor and last record."""
    size = lines = 0
    buffer = RecordBuffer(PI_RECORD_BYTES)
    last_complete: bytes | None = None
    with path.open("rb") as stream:
        while chunk := stream.read(PI_READ_BYTES):
            records = buffer.feed(chunk, size)
            size += len(chunk)
            lines += len(records)
            if records:
                last_complete = records[-1].raw
        stat = os.fstat(stream.fileno())
    projected = project_record(last_complete) if last_complete is not None else None
    last_line = projected.decode("utf-8") if projected else None
    return size, lines, stat.st_mtime_ns, last_line, stat.st_dev, stat.st_ino


def _switch_cursor(path: Path, boundary: PiSwitchBoundary) -> tuple[int, int] | None:
    """Resolve and validate a Pi-authored history boundary without unbounded buffering."""
    try:
        with path.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            if boundary.dev is not None and (
                stat.st_dev != boundary.dev or stat.st_ino != boundary.ino
            ):
                return None
            if boundary.offset is not None:
                if stat.st_size < boundary.offset:
                    return None
                remaining = boundary.offset
                records = 0
                last = b""
                while remaining:
                    chunk = stream.read(min(PI_READ_BYTES, remaining))
                    if not chunk:
                        return None
                    remaining -= len(chunk)
                    records += chunk.count(b"\n")
                    last = chunk[-1:]
                if boundary.offset and last != b"\n":
                    return None
                if boundary.records is not None and records != boundary.records:
                    return None
                return boundary.offset, records
            assert boundary.records is not None
            if boundary.records == 0:
                return 0, 0
            offset = records = 0
            while chunk := stream.read(PI_READ_BYTES):
                newlines = chunk.count(b"\n")
                if records + newlines >= boundary.records:
                    wanted = boundary.records - records
                    newline = -1
                    for _ in range(wanted):
                        newline = chunk.find(b"\n", newline + 1)
                    return offset + newline + 1, boundary.records
                records += newlines
                offset += len(chunk)
    except OSError:
        return None
    return None


class PiTranscriptSource(TranscriptSource):
    """A TranscriptSource with bounded reads and oversized-record recovery."""

    if TYPE_CHECKING:
        _observer: PiObserver

    def __init__(self, *args, **kwargs) -> None:
        self._source_checkpoint = kwargs.pop("source_checkpoint", None)
        super().__init__(*args, **kwargs)
        self._backlog: list[PiRecord] = []
        self._records = RecordBuffer(PI_RECORD_BYTES)
        self._read_size = 0
        #: Initial restart reconciliation parses only usage through this byte offset.
        self._usage_only_until: int | None = None
        self._pending_usage_only_until: int | None = None
        self._pending_checkpoint: str | None = None
        self._acknowledged_checkpoint: str | None = (
            self._source_checkpoint
            if _decode_checkpoint(self._source_checkpoint) is not None
            else None
        )
        self._stream_dev: int | None = None
        self._stream_ino: int | None = None
        self._pending_stream_dev: int | None = None
        self._pending_stream_ino: int | None = None

    def commit_attachment(self) -> None:
        super().commit_attachment()
        self._usage_only_until = self._pending_usage_only_until
        self._pending_usage_only_until = None
        self._stream_dev, self._stream_ino = self._pending_stream_dev, self._pending_stream_ino
        self._pending_stream_dev = self._pending_stream_ino = None
        self._clear_live_buffers()
        self._seed_live_context()

    def _history_reader(self):
        from .observer import PiObserver

        return stateful_history_reader(
            clone=lambda: PiObserver(root=self._observer.root, isolated=self._observer.isolated),
            seed_of=lambda observer: observer._seed_history_context,
            decorate=self._decorate_parsed,
        )

    async def _locate(self, *, session_id: str | None) -> Path | None:
        """Prefer the exact switch handoff emitted by Theater's bundled extension."""
        if session_id is None and self.path is not None and self._cwd:
            path = await asyncio.to_thread(
                self._observer.find_switch_transcript, cwd=self._cwd, current=self.path
            )
            if path is not None:
                return path if self._inside_domain(path) else None
        return await super()._locate(session_id=session_id)

    def discard_attachment(self) -> None:
        super().discard_attachment()
        self._pending_checkpoint = None
        self._pending_usage_only_until = None
        self._pending_stream_dev = self._pending_stream_ino = None
        self._seed_live_context()

    def revoke_attachment(self) -> None:
        super().revoke_attachment()
        self._usage_only_until = None
        self._pending_usage_only_until = None
        self._pending_checkpoint = None
        self._acknowledged_checkpoint = None
        self._pending_stream_dev = self._pending_stream_ino = None
        self._clear_live_buffers()
        self._observer._reset_turn_context()

    def _detach(self) -> None:
        super()._detach()
        self._usage_only_until = None
        self._pending_usage_only_until = None
        self._pending_checkpoint = None
        self._acknowledged_checkpoint = None
        self._stream_dev = self._stream_ino = None
        self._pending_stream_dev = self._pending_stream_ino = None
        self._clear_live_buffers()
        self._observer._reset_turn_context()

    def source_checkpoint(self) -> str | None:
        return self._acknowledged_checkpoint

    def pending_source_checkpoint(self) -> str | None:
        return self._pending_checkpoint

    def acknowledge_source_checkpoint(self) -> None:
        if self._pending_checkpoint is not None:
            self._acknowledged_checkpoint = self._pending_checkpoint
            self._source_checkpoint = self._pending_checkpoint
            self._pending_checkpoint = None

    def rollback_source_checkpoint(self) -> None:
        """Re-read an unpersisted batch from the most recent durable point."""
        checkpoint = _decode_checkpoint(self._acknowledged_checkpoint) or _decode_checkpoint(
            self._pending_checkpoint
        )
        self._pending_checkpoint = None
        self._clear_live_buffers()
        if checkpoint is None or self.path is None:
            return
        location, offset, records, dev, ino = checkpoint
        if location != str(self.path):
            self._detach()
            return
        try:
            stat = self.path.stat()
        except OSError:
            self._detach()
            return
        if stat.st_dev != dev or stat.st_ino != ino or stat.st_size < offset:
            self._detach()
            return
        self.offset, self.index, self.mtime = offset, records, stat.st_mtime_ns
        self._stream_dev, self._stream_ino = dev, ino
        self._seed_live_context()

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
        cursor = self._replay_cursor(path, size=size, lines=lines, dev=dev, ino=ino)
        if isinstance(cursor, _WaitForSwitch):
            return None
        offset, index = (size, lines) if cursor is None else cursor[:2]
        usage_only = cursor is not None and cursor[2]
        last_event: Event | None = None
        status: Status | None = None
        if last_line is not None:
            parsed = self._parse_record(last_line, max(0, lines - 1), clip_text=True)
            if cursor is None:
                semantic = [event for event in parsed.events if not event.usage_only]
                last_event = semantic[-1] if semantic else None
            if not usage_only:
                status = parsed.status
        self._pending_usage_only_until = size if usage_only else None
        self._pending_stream_dev, self._pending_stream_ino = dev, ino
        self._pending_checkpoint = _encode_checkpoint(path, offset, index, dev, ino)
        self._pending = (path, offset, index, mtime, session_id)
        return Attachment(
            location=str(path),
            session_id=session_id,
            # A usage-only reconciliation intentionally skips all control
            # records even while it replays their accounting payloads.
            skipped=lines if usage_only else index,
            last_event=last_event,
            status=status,
            point=StreamPoint(records=lines, size=size, dev=dev, ino=ino),
            correlation=self.correlation_for(path, session_id),
            collision_domain=self.collision_domain,
        )

    def _replay_cursor(
        self,
        path: Path,
        *,
        size: int,
        lines: int,
        dev: int | None,
        ino: int | None,
    ) -> tuple[int, int, bool] | _WaitForSwitch | None:
        """Return ``(offset, record_index, usage_only)`` for a safe replay.

        Cold and `/new` sessions replay from their trusted boundary. Restarts
        replay only usage from the durable checkpoint.
        """
        checkpoint = _decode_checkpoint(self._source_checkpoint)
        if checkpoint is not None:
            if _checkpoint_matches(checkpoint, path, size=size, lines=lines, dev=dev, ino=ino):
                return (checkpoint[1], checkpoint[2], True)
            if checkpoint[0] != str(path):
                return self._rotation_cursor(path)
            return None
        if not self._exact_attachments:
            return None
        if self.path is not None:
            return self._rotation_cursor(path)
        if self._observer.is_fork_transcript(path):
            if not self._cwd:
                return None
            boundary = self._observer.switch_boundary(cwd=self._cwd, target=path)
            if boundary is None or boundary.reason != "startup-fork" or boundary.location != path:
                # Pi creates the fork file before extension session_start
                # records the copied-history boundary. Never race ahead and
                # replay the copied prefix while that handoff is pending.
                return _WAIT_FOR_SWITCH
            cursor = _switch_cursor(path, boundary)
            if cursor is None:
                return _WAIT_FOR_SWITCH
            return (*cursor, self._known_location is not None)
        floor_cursor = self._floor_cursor(
            self._source_checkpoint,
            size=size,
            lines=lines,
            dev=dev,
            ino=ino,
        )
        if floor_cursor is None:
            return None
        offset, index = floor_cursor
        if self._known_location is not None:
            return (offset, index, True)
        return (offset, index, False)

    def _rotation_cursor(self, path: Path) -> tuple[int, int, bool] | _WaitForSwitch | None:
        """Honor Pi's explicit switch boundary; unknown rotations attach at EOF."""
        if self.path is None or not self._cwd:
            return None
        boundary = self._observer.switch_boundary(cwd=self._cwd, current=self.path, target=path)
        if boundary is None or boundary.location != path:
            return None
        if boundary.reason == "new":
            return (0, 0, False)
        cursor = _switch_cursor(path, boundary)
        if cursor is None:
            return _WAIT_FOR_SWITCH
        offset, records = cursor
        return (offset, records, False)

    def _floor_cursor(
        self,
        raw: str | None,
        *,
        size: int,
        lines: int,
        dev: int | None,
        ino: int | None,
    ) -> tuple[int, int] | None:
        """Return the cold or validated resumed cursor; fail closed otherwise."""
        if raw is None:
            return (0, 0)
        floor = decode_floor(raw)
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

    async def _drain(self) -> Batch:
        if self._backlog:
            return await self._drain_records()
        assert self.path is not None
        path, offset, index, mtime = self.path, self.offset, self.index, self.mtime
        stat = path.stat()
        if stat.st_size < offset or (stat.st_size == offset and stat.st_mtime_ns != mtime):
            offset = index = 0
            self._usage_only_until = None
            self._stream_dev, self._stream_ino = stat.st_dev, stat.st_ino
            self._pending_checkpoint = _encode_checkpoint(path, 0, 0, stat.st_dev, stat.st_ino)
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
        self._read_size = read_stat.st_size
        self.index = index
        self._backlog.extend(self._records.feed(data, offset))
        batch = await self._drain_records()
        return replace(batch, progressed=True)

    async def _drain_records(self) -> Batch:
        records = self._backlog[:PI_RECORDS_PER_BATCH]
        if not records:
            return Batch(has_more=self.offset < self._read_size)
        parsed_records, state, oversized = await asyncio.to_thread(
            parse_live_records,
            self._observer._snapshot_turn_context(),
            [record.raw for record in records],
            self.index,
        )
        self._observer._restore_turn_context(state)
        del self._backlog[:PI_RECORDS_PER_BATCH]
        events: list[Event] = []
        trajectory: list[TrajectoryFact] = []
        trajectory_events: list[Event] = []
        status: Status | None = None
        next_checkpoint: str | None = None
        for record, parsed in zip(records, parsed_records, strict=True):
            decorated = self._decorate_parsed(parsed, record.start)
            if self._usage_only_until is not None and record.start < self._usage_only_until:
                events.extend(self._usage_only(event) for event in decorated.events if event.usage)
            else:
                events.extend(decorated.events)
                trajectory.extend(decorated.trajectory)
                trajectory_events.extend(decorated.baseline_events)
                status = self._advance_status_hint(status, decorated)
            self.index += 1
            assert self.path is not None
            next_checkpoint = _encode_checkpoint(
                self.path,
                record.end,
                self.index,
                self._stream_dev,
                self._stream_ino,
            )
        if next_checkpoint is not None:
            self._pending_checkpoint = next_checkpoint
        return Batch(
            events=events,
            progressed=bool(records),
            has_more=bool(self._backlog) or self.offset < self._read_size,
            status=status,
            trajectory=trajectory,
            trajectory_events=trajectory_events,
            error_code="pi_transcript_oversized_record" if oversized else None,
            error="Pi record exceeded the raw or projected structural limit" if oversized else None,
        )

    def _clear_live_buffers(self) -> None:
        self._backlog.clear()
        self._records.clear()
        self._read_size = 0

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
