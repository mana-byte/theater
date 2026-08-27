"""Reusable identity callbacks for file-backed transcript plugins.

A compiled manifest observer inherits the bare ``HarnessObserver`` defaults,
not ``TranscriptObserver``'s file-backed ones, so a plugin whose transcript
lives on disk must wire these explicitly rather than rely on inheritance.
"""

from __future__ import annotations

from pathlib import Path

from theater.harness.contracts.callbacks import StreamFloorContext
from theater.harness.contracts.source import StreamPoint


def file_stream_floor(context: StreamFloorContext) -> StreamPoint | None:
    """Capture the stream position of a file-backed transcript.

    Reads the file once and returns a :class:`StreamPoint` carrying the
    record count, byte size, and the device/inode from the same descriptor.
    Returns ``None`` when the location is not a readable file — an
    unavailable floor is represented as ``None`` rather than a partial fact,
    so the spawner persists a present-but-unknown floor instead of one that
    could be confused with a cold spawn.
    """
    from theater.harness.transcript.attachment import attach_point

    try:
        size, lines, _mtime, _last_line, dev, ino = attach_point(Path(context.location))
    except OSError:
        return None
    return StreamPoint(records=lines, size=size, dev=dev, ino=ino)


__all__ = ["file_stream_floor"]
