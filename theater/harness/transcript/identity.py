"""Reusable identity callbacks for file-backed transcript plugins.

Compiled manifest observers do not inherit ``TranscriptObserver``, so wire these explicitly.
"""

from __future__ import annotations

from pathlib import Path

from theater.harness.contracts.callbacks import StreamFloorContext
from theater.harness.contracts.source import StreamPoint


def file_stream_floor(context: StreamFloorContext) -> StreamPoint | None:
    """Capture the stream position of a file-backed transcript.

    ``None`` when unreadable, never a partial fact, so it is not confused with a cold spawn.
    """
    from theater.harness.transcript.attachment import attach_point

    try:
        size, lines, _mtime, _last_line, dev, ino = attach_point(Path(context.location))
    except OSError:
        return None
    return StreamPoint(records=lines, size=size, dev=dev, ino=ino)


__all__ = ["file_stream_floor"]
