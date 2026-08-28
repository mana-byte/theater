"""Declarative skill discovery and loading for agents."""

from __future__ import annotations

from theater.mcp.session import Session


async def list_skills(session: Session) -> dict:
    result = await session.client.call("skills.list")
    assert isinstance(result, dict)
    return result


async def load_skill(session: Session, *, name: str) -> dict:
    result = await session.client.call("skills.load", name=name)
    assert isinstance(result, dict)
    return result
