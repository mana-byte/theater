"""Transcript attachment helper: byte offset, record count, mtime, stream identity.

Extracted from the former ``theater.harness.source`` module as the
transcript-file implementation of the initial attach scan.
"""

from __future__ import annotations

import os
from pathlib import Path

from theater.constants.harness import HARNESS_TRANSCRIPT_SCAN_CHUNK_BYTES


def attach_point(path: Path) -> tuple[int, int, int, str | None, int | None, int | None]:
    """Byte offset, record count, mtime, last complete line, dev, ino at end of file.

    The mtime is taken *after* the read, from the same descriptor, so it always
    covers every byte counted here even if a writer appended mid-scan.

    The last complete line is returned so the caller can derive an initial
    status from it without replaying history onto the bus. A spawned agent
    that finishes its turn before the observer attaches would otherwise keep
    the wrong status: no new bytes arrive after attach, so nothing else fires.

    The device and inode are taken from the same ``fstat`` as the mtime, so
    they describe the file the bytes were read from — not a later ``stat``
    that can race a rename or replacement. They are the opaque stream identity
    a resume floor checks against: a truncated-and-rewritten file has a
    different inode, and a file on a different device is a different stream.
    """
    size = 0
    lines = 0
    tail: list[bytes] = []
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(HARNESS_TRANSCRIPT_SCAN_CHUNK_BYTES), b""):
            size += len(chunk)
            lines += chunk.count(b"\n")
            tail.append(chunk)
        st = os.fstat(fh.fileno())
        mtime = st.st_mtime_ns
        dev = st.st_dev
        ino = st.st_ino
    last_line: str | None = None
    if lines > 0:
        data = b"".join(tail)
        head, sep, _rest = data.rpartition(b"\n")
        if sep:
            # head is before the last newline; the last complete line follows it.
            _prefix, _sep2, last_bytes = head.rpartition(b"\n")
            last_line = last_bytes.decode("utf-8", errors="replace")
    return size, lines, mtime, last_line, dev, ino
