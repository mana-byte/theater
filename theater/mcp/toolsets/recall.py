"""File history tools: who last changed these files, and the story behind it.

``recall`` returns per-file timelines; ``recall_read`` expands one point of a
timeline into its full transcript or git-history explanation.
"""

from __future__ import annotations

from pathlib import Path

from theater.mcp.session import Session


async def recall(session: Session, *, paths: list[str], depth: int = 5) -> dict[str, dict]:
    """Per-file timelines of what Theater watched happen.

    Returns one timeline per path, keyed by the repo-relative path.
    Each job point carries a ``session_id`` that composes with
    ``spawn_session(resume=<session_id>)`` — the session id out of
    ``recall`` goes straight into ``resume``.

    Paths may be absolute or repo-relative; they are normalised to
    repo-relative before querying, since that is how they are stored.
    A path that has never been touched comes back as an empty timeline.
    """
    if not session._resolved:
        await session.identify()
    result = await session.client.call(
        "recall",
        paths=paths,
        depth=depth,
        caller_cwd=str(Path.cwd()),
    )
    assert isinstance(result, dict)
    return result


async def recall_read(session: Session, *, segment_id: str) -> dict:
    """Explain one point of a `recall` timeline.

    Takes the ``segment`` id off a timeline point. A job segment comes
    back with the job's transcript; a gap segment comes back with the
    commits git can find for that blob transition, or an explicit
    ``explained: false`` when it can find none — which is a different
    answer from an empty list.

    Separate from `recall` because it is the only call in the feature
    that spends a ``git log``, and because it answers about one segment
    rather than about paths.
    """
    if not session._resolved:
        await session.identify()
    result = await session.client.call(
        "recall_read",
        segment_id=segment_id,
        caller_cwd=str(Path.cwd()),
    )
    assert isinstance(result, dict)
    return result
