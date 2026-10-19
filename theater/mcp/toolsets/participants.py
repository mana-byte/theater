"""Participant identity and listing tools.

These are the tools an agent calls to learn who it is and who else is on the
machine. ``_summarise`` is shared with the delegation toolset.
"""

from __future__ import annotations

from theater.constants.daemon import PARTICIPANTS_LIST_DEFAULT_DEAD_LIMIT
from theater.mcp.session import Session
from theater.mcp.toolsets.projection import compact

#: whoami lists at most this many live direct children; children_total reports the rest.
WHOAMI_CHILDREN_CAP = 20


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
            "model": p.get("model"),
            "reasoning_effort": p.get("reasoning_effort"),
            "play": p.get("play"),
        }
    )


async def whoami(session: Session, *, include_git: bool = False) -> dict:
    if not session._resolved:
        await session.identify()
    # The self view adds what this agent may spawn in its live play.
    params: dict[str, object] = {"id": session.participant_id, "self_view": True}
    if include_git:
        params["include_git"] = True
    record = await session.client.call("participants.get", **params)
    assert isinstance(record, dict)
    me = _summarise(record)
    if include_git:
        # Kept whole: a null branch (detached HEAD) or reason is the answer, not noise.
        me["git"] = record["git"]
    rows = await session.client.call("participants.list", parent_id=me["id"])
    assert isinstance(rows, list)
    children = [
        compact({"name": p.get("name"), "status": p.get("status"), "harness": p.get("harness")})
        for p in rows[:WHOAMI_CHILDREN_CAP]
    ]
    if children:
        me["children"] = children
    if len(rows) > WHOAMI_CHILDREN_CAP:
        me["children_total"] = len(rows)
    return me


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
    if include_dead:
        params["order"] = "newest"
    if limit is not None:
        params["limit"] = limit
    elif include_dead and ids is None:
        params["limit"] = PARTICIPANTS_LIST_DEFAULT_DEAD_LIMIT
    if after_id is not None:
        params["after_id"] = after_id
    rows = await session.client.call("participants.list", **params)
    assert isinstance(rows, list)
    me = session.participant_id
    out = []
    for p in rows:
        row = _summarise(p)
        if p["id"] == me:
            row["is_self"] = True
        if p["resume_state"] != "live":
            row["resume_state"] = p["resume_state"]
        if p["status"] == "dead" and row.get("addressable") is False:
            del row["addressable"]
        out.append(row)
    page = params.get("limit")
    if out and ids is None and isinstance(page, int) and len(out) >= page:
        # A full page may hide more rows; the cursor makes that visible without an error.
        out[-1]["next_after_id"] = out[-1]["id"]
    return out


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
