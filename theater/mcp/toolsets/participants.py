"""Participant identity and listing tools.

These are the tools an agent calls to learn who it is and who else is on the
machine. ``_summarise`` is shared with the delegation toolset.
"""

from __future__ import annotations

from pathlib import Path

from theater.constants.daemon import PARTICIPANTS_LIST_DEFAULT_DEAD_LIMIT
from theater.mcp.session import Session
from theater.mcp.toolsets.projection import compact


def _summarise(p: dict) -> dict:
    """Trim a participant record to what another agent needs to route work.

    Empty fields are omitted. ``name`` is a live alias unique among live participants,
    so any tool accepts it in place of the id; only a dead participant needs its id.
    """
    return compact(
        {
            "id": p["id"],
            "name": p["name"],
            "description": p["description"],
            "harness": p["harness"],
            "tier": p["tier"],
            "status": p["status"],
            "cwd": p["cwd"],
            "branch": p["branch"],
            "session_id": p["session_id"],
            "parent_id": p["parent_id"],
            "addressable": p["addressable"],
            "tmux_server_identity": p.get("tmux_server_identity"),
            "termination_reason": p.get("termination_reason"),
            "termination_incident": p.get("termination_incident"),
            "terminated_at": p.get("terminated_at"),
        }
    )


async def whoami(session: Session) -> dict:
    return _summarise(await session.me())


async def list_participants(
    session: Session,
    *,
    include_dead: bool = False,
    ids: list[str] | None = None,
    children_only: bool = False,
    limit: int | None = None,
    after_id: str | None = None,
) -> list[dict]:
    if not session._resolved:
        await session.identify()
    params: dict[str, object] = {
        "include_dead": include_dead,
        "ids": ids,
        "parent_id": session.participant_id if children_only else None,
    }
    if limit is not None:
        params["limit"] = limit
    elif include_dead and ids is None:
        params["limit"] = PARTICIPANTS_LIST_DEFAULT_DEAD_LIMIT
    if after_id is not None:
        params["after_id"] = after_id
    rows = await session.client.call("participants.list", **params)
    assert isinstance(rows, list)
    me = session.participant_id
    return [
        {**_summarise(p), "is_self": p["id"] == me, "resume_state": p["resume_state"]} for p in rows
    ]


async def register_pane(session: Session, *, pane: str) -> dict:
    """Call the retained private endpoint, which refuses unsafe pane-only adoption."""
    record = await session.client.call(
        "hello",
        id=session.participant_id,
        harness=session.harness,
        pane=pane,
        cwd=str(Path.cwd()),
    )
    assert isinstance(record, dict)
    session.participant_id = record["id"]
    session._resolved = True
    return _summarise(record)


async def update_participant(
    session: Session,
    *,
    target: str | None = None,
    name: str | None = None,
    description: str | None = None,
) -> dict:
    """Update this participant or one direct child through the daemon."""
    if not session._resolved:
        await session.identify()
    record = await session.client.call(
        "participant.update",
        caller_id=session.participant_id,
        target=target,
        name=name,
        description=description,
    )
    assert isinstance(record, dict)
    return _summarise(record)
