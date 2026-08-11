"""Tool bodies, kept free of any MCP framework so they can be tested directly.

Identity resolution happens here, once, on the first call. It is deliberately
lazy: the daemon may not exist yet when the harness starts this process, and
failing at import time would show up to the user as "MCP server crashed" with no
explanation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from theater.client import DaemonClient


@dataclass(slots=True)
class Session:
    """One MCP server process, acting on behalf of exactly one participant."""

    participant_id: str | None
    harness: str
    client: DaemonClient
    _resolved: bool = False

    async def identify(self) -> dict:
        """Announce ourselves to the daemon and cache the resulting record.

        `pane` is read from $TMUX_PANE, which usually is not there: the MCP SDK
        replaces the inherited environment with a six-variable allowlist unless
        the harness config says otherwise. When it is missing and no id was
        given on argv, the daemon files us as External — correct, since without a
        pane nobody can type into us.
        """
        record = await self.client.call(
            "hello",
            id=self.participant_id,
            harness=self.harness,
            pane=os.environ.get("TMUX_PANE"),
            cwd=os.getcwd(),
        )
        assert isinstance(record, dict)
        self.participant_id = record["id"]
        self._resolved = True
        return record

    async def me(self) -> dict:
        if not self._resolved:
            return await self.identify()
        record = await self.client.call("participants.get", id=self.participant_id)
        assert isinstance(record, dict)
        return record


def _summarise(p: dict) -> dict:
    """Trim a participant record to what another agent needs to route work.

    Everything here answers one of: who are you, where are you, can I reach you.
    """
    return {
        "id": p["id"],
        "harness": p["harness"],
        "tier": p["tier"],
        "status": p["status"],
        "cwd": p["cwd"],
        "branch": p["branch"],
        "parent_id": p["parent_id"],
        "addressable": p["addressable"],
    }


async def whoami(session: Session) -> dict:
    return _summarise(await session.me())


async def list_participants(session: Session, *, include_dead: bool = False) -> list[dict]:
    if not session._resolved:
        await session.identify()
    rows = await session.client.call("participants.list", include_dead=include_dead)
    assert isinstance(rows, list)
    me = session.participant_id
    return [{**_summarise(p), "is_self": p["id"] == me} for p in rows]


async def spawn_session(
    session: Session,
    *,
    harness: str,
    prompt: str,
    approval: str,
    cwd: str | None = None,
) -> dict:
    """Create a child agent in a new tmux window and return its record.

    The prompt is delivered on the child's argv, not by typing into its pane, so
    this path does not depend on keystroke injection working at all.
    """
    if not session._resolved:
        await session.identify()
    record = await session.client.call(
        "spawn",
        harness=harness,
        prompt=prompt,
        approval=approval,
        cwd=cwd or os.getcwd(),
        parent_id=session.participant_id,
        tmux_session=os.environ.get("THEATER_TMUX_SESSION"),
    )
    assert isinstance(record, dict)
    return _summarise(record)


async def register_pane(session: Session, *, pane: str) -> dict:
    """Adoption fallback: the agent looked up its own $TMUX_PANE and tells us.

    Needed because the MCP environment allowlist hides TMUX_PANE from this
    process. An agent can still read it from its own shell tool.
    """
    record = await session.client.call(
        "hello",
        id=session.participant_id,
        harness=session.harness,
        pane=pane,
        cwd=os.getcwd(),
    )
    assert isinstance(record, dict)
    session.participant_id = record["id"]
    session._resolved = True
    return _summarise(record)


async def await_sessions(
    session: Session, *, handles: list[str], max_wait: float = 60.0
) -> list[dict]:
    """Wait for spawned child sessions to finish, up to max_wait seconds.

    Returns the current state of each job. A job that is still running when
    the timeout expires is returned with state="running" — re-await if you
    want to keep waiting. The result text is the assistant's final response
    from the child's turn.

    This blocks the current MCP request only; the daemon and every other
    participant continue running.
    """
    if not session._resolved:
        await session.identify()
    jobs = await session.client.call(
        "jobs.await", handles=handles, max_wait=max_wait
    )
    assert isinstance(jobs, list)
    return jobs
