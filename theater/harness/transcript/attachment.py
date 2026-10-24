"""Transcript attachment scan: end offset, record count, stream identity and a bounded tail.

Extracted from the former ``theater.harness.source`` module as the
transcript-file implementation of the initial attach scan.
"""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from theater.constants.harness import HARNESS_TRANSCRIPT_SCAN_CHUNK_BYTES


class TailRecord(NamedTuple):
    """One complete record at the end of the scanned file."""

    #: Absolute record index, named like ``Event.raw_index`` (``tuple.index`` is taken).
    raw_index: int
    #: Byte offset just past the record's newline.
    end: int
    #: Decoded without its newline.
    line: str


@dataclass(frozen=True, slots=True)
class AttachSnapshot:
    """What one forward pass over a transcript saw: counts, identity and the last records."""

    #: Bytes read, including any incomplete trailing record.
    size: int
    #: Complete records (newlines).
    lines: int
    mtime: int
    dev: int
    ino: int
    #: Bytes after the last newline: a record still being written.
    trailing_bytes: int
    #: The last complete records, oldest first; empty when none was requested.
    tail: tuple[TailRecord, ...]

    @property
    def last_line(self) -> str | None:
        """The last complete line, which seeds status for agents that finished before attach."""
        return self.tail[-1].line if self.tail else None


def attach_point(path: Path, tail: int = 1) -> AttachSnapshot:
    """Scan ``path`` once, keeping its last ``tail`` complete records.

    mtime/dev/ino come from the same fd after reading, so they cover every counted byte and
    cannot race a rename. Memory is bounded by the records asked for, not the file: chunks are
    dropped once the rest hold ``tail + 1`` newlines, so only the last ``tail`` records, the
    chunk straddling the oldest, and an unfinished trailing record are resident.
    """
    size = lines = last_end = 0
    kept: deque[tuple[bytes, int]] = deque()  # (chunk, newlines in it)
    kept_newlines = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(HARNESS_TRANSCRIPT_SCAN_CHUNK_BYTES), b""):
            newlines = chunk.count(b"\n")
            if newlines:
                last_end = size + chunk.rfind(b"\n") + 1
            if tail:
                kept.append((chunk, newlines))
                kept_newlines += newlines
                while kept_newlines - kept[0][1] > tail:
                    kept_newlines -= kept.popleft()[1]
            size += len(chunk)
            lines += newlines
        st = os.fstat(fh.fileno())
    # Retained bytes either start at the top of the file or hold tail + 1 newlines, so a record cut
    # by a chunk edge can only be the piece before the last ``tail`` ones.
    pieces = b"".join(chunk for chunk, _ in kept).split(b"\n")[:-1][-tail:] if tail else []
    records: list[TailRecord] = []
    end = last_end
    for raw in reversed(pieces):
        records.append(TailRecord(lines - 1 - len(records), end, raw.decode("utf-8", "replace")))
        end -= len(raw) + 1
    return AttachSnapshot(
        size=size,
        lines=lines,
        mtime=st.st_mtime_ns,
        dev=st.st_dev,
        ino=st.st_ino,
        trailing_bytes=size - last_end,
        tail=tuple(reversed(records)),
    )
