"""Transcript reading tools.

The agent-facing ``await_sessions`` reply drops prompt and result text; this
is where an agent goes to get the full, unclipped text instead.
"""

from __future__ import annotations

from theater.mcp.session import Session


async def read_transcript(session: Session, *, target_id: str, last_n: int = 5) -> dict:
    """Read the transcript of a participant, returning full unclipped text.

    The agent-facing await_sessions reply drops prompt and result text.
    This method returns the full assistant responses from the transcript on
    disk, so a caller that needs the complete text can get it.

    ``target_id`` may be a participant id or a name. Names work only
    while the participant is live; a dead participant's name is null
    and cannot be resolved. Use the id to read the transcript of a dead
    participant — the id is the stable reference for as long as the row
    is retained (historical access is retention-bounded; dead rows are
    eventually deleted by GC).

    Returns the last `last_n` events (user, assistant, tool_call,
    tool_result) from the transcript, in chronological order. Each entry
    has `role`, `text` (full, unclipped), `tool_name`, and `turn_end`.
    """
    if not session._resolved:
        await session.identify()
    record = await session.client.call("read_transcript", id=target_id, last_n=last_n)
    assert isinstance(record, dict)
    return record
