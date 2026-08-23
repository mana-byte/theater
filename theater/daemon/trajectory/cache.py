"""Bounded trajectory record rings and participant-stream LRU storage."""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from theater.constants.trajectory import (
    TRAJECTORY_IDLE_TTL_SECONDS,
    TRAJECTORY_PARTICIPANT_CACHE_MAX_BYTES,
    TRAJECTORY_TOTAL_CACHE_MAX_BYTES,
    TRAJECTORY_WARM_STREAM_LIMIT,
)
from theater.trajectory import TrajectoryRecord, merge_records, newer_record


def encoded_record_bytes(record: TrajectoryRecord) -> int:
    """Return the compact UTF-8 bytes accounted to one cached record."""
    return len(
        json.dumps(record.to_wire(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


@dataclass(frozen=True, slots=True)
class RecordChange:
    sequence: int
    record: TrajectoryRecord


@dataclass(frozen=True, slots=True)
class RingRead:
    changes: tuple[RecordChange, ...] = ()
    current_sequence: int = 0
    resync_required: bool = False
    reason: str | None = None


@dataclass(slots=True)
class _Entry:
    record: TrajectoryRecord
    size: int
    sequence: int


class RecordRing:
    """An insertion-ordered, byte-accounted ring of exact record identities."""

    def __init__(self, max_bytes: int = TRAJECTORY_PARTICIPANT_CACHE_MAX_BYTES):
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("trajectory ring max_bytes must be a positive integer")
        self.max_bytes = max_bytes
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._bytes = 0
        self._sequence = 0
        self._floor_sequence = 0

    @property
    def byte_size(self) -> int:
        return self._bytes

    @property
    def current_sequence(self) -> int:
        return self._sequence

    @property
    def floor_sequence(self) -> int:
        return self._floor_sequence

    def __len__(self) -> int:
        return len(self._entries)

    def records(self) -> tuple[TrajectoryRecord, ...]:
        return tuple(entry.record for entry in self._entries.values())

    def get(self, record_id: str) -> TrajectoryRecord | None:
        entry = self._entries.get(record_id)
        return entry.record if entry is not None else None

    def merge(self, records: Iterable[TrajectoryRecord]) -> tuple[RecordChange, ...]:
        """Merge exact IDs and retain only newer revisions."""
        changes: list[RecordChange] = []
        for candidate in merge_records((), records):
            existing = self._entries.get(candidate.record_id)
            if existing is not None:
                selected = newer_record(existing.record, candidate)
                if selected is existing.record:
                    continue
                self._bytes -= existing.size
                self._sequence += 1
                replacement = _Entry(selected, encoded_record_bytes(selected), self._sequence)
                self._entries[candidate.record_id] = replacement
                self._entries.move_to_end(candidate.record_id)
                self._bytes += replacement.size
                changes.append(RecordChange(self._sequence, selected))
                self._evict()
                continue
            self._sequence += 1
            entry = _Entry(candidate, encoded_record_bytes(candidate), self._sequence)
            self._entries[candidate.record_id] = entry
            self._bytes += entry.size
            changes.append(RecordChange(self._sequence, candidate))
            self._evict()
        return tuple(changes)

    def changes_after(self, sequence: int, *, limit: int) -> RingRead:
        """Read current revisions after a cursor without hiding ring gaps."""
        if type(sequence) is not int or sequence < 0:
            return RingRead(
                current_sequence=self._sequence,
                resync_required=True,
                reason="trajectory cursor sequence is invalid; request a fresh snapshot",
            )
        if type(limit) is not int or limit <= 0:
            raise ValueError("trajectory follow limit must be a positive integer")
        if sequence > self._sequence:
            return RingRead(
                current_sequence=self._sequence,
                resync_required=True,
                reason="trajectory cursor is ahead of this stream; request a fresh snapshot",
            )
        if sequence < self._floor_sequence:
            return RingRead(
                current_sequence=self._sequence,
                resync_required=True,
                reason="trajectory history fell out of the daemon ring; request a fresh snapshot",
            )
        changes = tuple(
            RecordChange(entry.sequence, entry.record)
            for entry in self._entries.values()
            if entry.sequence > sequence
        )
        return RingRead(changes=changes[:limit], current_sequence=self._sequence)

    def _evict(self) -> None:
        while self._bytes > self.max_bytes and self._entries:
            _record_id, entry = self._entries.popitem(last=False)
            self._bytes -= entry.size
            self._floor_sequence = max(self._floor_sequence, entry.sequence)


@dataclass(slots=True)
class CacheStream:
    participant_id: str
    stream_id: str
    ring: RecordRing
    last_used: float
    viewer_refs: int = 0
    follower_refs: int = 0
    loading: bool = False


class TrajectoryCache:
    """Participant-stream LRU with independent ring and aggregate byte limits."""

    def __init__(
        self,
        *,
        participant_bytes: int = TRAJECTORY_PARTICIPANT_CACHE_MAX_BYTES,
        total_bytes: int = TRAJECTORY_TOTAL_CACHE_MAX_BYTES,
        warm_streams: int = TRAJECTORY_WARM_STREAM_LIMIT,
        idle_ttl: float = TRAJECTORY_IDLE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        if type(participant_bytes) is not int or participant_bytes <= 0:
            raise ValueError("participant_bytes must be a positive integer")
        if type(total_bytes) is not int or total_bytes <= 0:
            raise ValueError("total_bytes must be a positive integer")
        if type(warm_streams) is not int or warm_streams <= 0:
            raise ValueError("warm_streams must be a positive integer")
        if type(idle_ttl) not in (int, float) or idle_ttl <= 0:
            raise ValueError("idle_ttl must be a positive number")
        self.participant_bytes = participant_bytes
        self.total_bytes = total_bytes
        self.warm_streams = warm_streams
        self.idle_ttl = float(idle_ttl)
        self._clock = clock
        self._entries: OrderedDict[str, CacheStream] = OrderedDict()

    @property
    def total_cached_bytes(self) -> int:
        return sum(entry.ring.byte_size for entry in self._entries.values())

    @property
    def warm_count(self) -> int:
        return len(self._entries)

    def get(self, participant_id: str) -> CacheStream | None:
        return self.touch(participant_id)

    def add(self, participant_id: str, stream_id: str) -> CacheStream:
        if participant_id in self._entries:
            raise ValueError(f"trajectory cache already has participant {participant_id!r}")
        now = self._clock()
        entry = CacheStream(
            participant_id=participant_id,
            stream_id=stream_id,
            ring=RecordRing(self.participant_bytes),
            last_used=now,
        )
        self._entries[participant_id] = entry
        self.touch(participant_id, now=now)
        return entry

    def touch(self, participant_id: str, *, now: float | None = None) -> CacheStream | None:
        entry = self._entries.get(participant_id)
        if entry is None:
            return None
        entry.last_used = self._clock() if now is None else now
        self._entries.move_to_end(participant_id)
        return entry

    def remove(self, participant_id: str) -> CacheStream | None:
        return self._entries.pop(participant_id, None)

    def evictable(self, entry: CacheStream) -> bool:
        """Whether a stream may be removed by a bound or TTL sweep."""
        return not entry.loading

    def enforce(self, *, protected: set[str] | None = None) -> tuple[CacheStream, ...]:
        """Evict least-recently-used idle streams until all bounds hold."""
        protected = protected or set()
        evicted: list[CacheStream] = []
        while len(self._entries) > self.warm_streams or self.total_cached_bytes > self.total_bytes:
            candidate = next(
                (
                    entry
                    for pid, entry in self._entries.items()
                    if pid not in protected and self.evictable(entry)
                ),
                None,
            )
            if candidate is None:
                break
            self._entries.pop(candidate.participant_id, None)
            evicted.append(candidate)
        return tuple(evicted)

    def expire(self, *, now: float | None = None) -> tuple[CacheStream, ...]:
        current = self._clock() if now is None else now
        expired = tuple(
            entry
            for entry in self._entries.values()
            if current - entry.last_used >= self.idle_ttl and self.evictable(entry)
        )
        for entry in expired:
            self._entries.pop(entry.participant_id, None)
        return expired


__all__ = [
    "CacheStream",
    "RecordChange",
    "RecordRing",
    "RingRead",
    "TrajectoryCache",
    "encoded_record_bytes",
]
