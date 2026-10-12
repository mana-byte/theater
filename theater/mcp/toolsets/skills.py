"""Declarative skill discovery and loading for agents."""

from __future__ import annotations

from theater.mcp.session import Session


async def list_skills(session: Session, *, play: str | None = None) -> dict:
    result = await session.client.call("skills.list", grouped=True, play=play)
    assert isinstance(result, dict)
    return result


async def load_skill(session: Session, *, name: str) -> dict:
    """Loading a play's master skill starts that play for this agent, so it is identified first."""
    if not session._resolved:
        await session.identify()
    result = await session.client.call("skills.load", name=name, caller_id=session.participant_id)
    assert isinstance(result, dict)
    return result
