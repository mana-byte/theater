"""Declarative skill discovery and loading for agents."""

from __future__ import annotations

from theater.mcp.session import Session


async def list_skills(session: Session, *, play: str | None = None) -> dict:
    result = await session.client.call("skills.list", grouped=True, play=play)
    assert isinstance(result, dict)
    return result


async def load_skill(session: Session, *, name: str, preview: bool = False) -> dict:
    """Preview omits identity so even an older daemon cannot start a play."""
    if preview:
        result = await session.client.call("skills.load", name=name, preview=True)
    else:
        caller_id = await session.caller_id()
        result = await session.client.call("skills.load", name=name, caller_id=caller_id)
    assert isinstance(result, dict)
    return result
