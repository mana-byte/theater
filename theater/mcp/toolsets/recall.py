"""File history tools: who last changed these files, and the story behind it.

``recall`` returns per-file timelines; ``recall_read`` expands one point of a
timeline into bounded transcript details or a git-history explanation.
"""

from __future__ import annotations

from pathlib import Path

from theater.mcp.session import Session


async def recall(session: Session, *, paths: list[str], depth: int = 5) -> dict[str, dict]:
    """Request per-file timelines and remove private job handles."""
    await session.caller_id()
    result = await session.client.call(
        "recall",
        paths=paths,
        depth=depth,
        caller_cwd=str(Path.cwd()),
    )
    assert isinstance(result, dict)
    for record in result.values():
        for point in record.get("timeline", []):
            point.pop("handle", None)
    return result


async def recall_read(session: Session, *, segment_id: str) -> dict:
    """Request one bounded transcript or Git-history segment."""
    await session.caller_id()
    result = await session.client.call(
        "recall_read",
        segment_id=segment_id,
        caller_cwd=str(Path.cwd()),
    )
    assert isinstance(result, dict)
    return result
