"""Bounded JSONL framing with original byte coordinates, including skipped records."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PiRecord:
    raw: bytes | None
    start: int
    end: int


class RecordBuffer:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._partial = bytearray()
        self._start: int | None = None
        self._dropping = False

    def clear(self) -> None:
        self._partial.clear()
        self._start = None
        self._dropping = False

    def feed(self, data: bytes, offset: int) -> list[PiRecord]:
        records: list[PiRecord] = []
        position = 0
        while position < len(data):
            if self._start is None:
                self._start = offset + position
            newline = data.find(b"\n", position)
            end = len(data) if newline < 0 else newline
            if not self._dropping:
                if len(self._partial) + end - position > self._limit:
                    self._partial.clear()
                    self._dropping = True
                else:
                    self._partial.extend(data[position:end])
            if newline < 0:
                break
            records.append(
                PiRecord(
                    None if self._dropping else bytes(self._partial),
                    self._start,
                    offset + newline + 1,
                )
            )
            self.clear()
            position = newline + 1
        return records
