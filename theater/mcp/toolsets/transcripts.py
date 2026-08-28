"""Bounded transcript reading for agents."""

from __future__ import annotations

from theater.mcp.session import Session


async def read_transcript(session: Session, *, target: str, cursor: str | None = None) -> dict:
    """Read one bounded newest transcript page, then page toward older content.

    Call once with a known live name or stable id, inspect the newest bounded
    chunk, and use only its next_cursor when older content is necessary.
    Stop when the needed event is found. Do not list participants first.

    target accepts a participant id or a current live name directly. Dead
    names are cleared and recyclable, so reading a dead participant requires
    its stable id while the retained row exists. cursor is an opaque value
    returned by Theater; omit it for the newest page.
    """
    if not session._resolved:
        await session.identify()
    params = {"id": target}
    if cursor is not None:
        params["cursor"] = cursor
    record = await session.client.call("read_transcript", **params)
    assert isinstance(record, dict)
    return record
