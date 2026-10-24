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
        snapshot = attach_point(Path(context.location), tail=0)
    except OSError:
        return None
    return StreamPoint(
        records=snapshot.lines, size=snapshot.size, dev=snapshot.dev, ino=snapshot.ino
    )


__all__ = ["file_stream_floor"]
