"""Transcript reading tools.

The agent-facing ``await_sessions`` reply drops prompt and result text; this
is where an agent goes to get the full, unclipped text instead.
"""

from __future__ import annotations

from theater.mcp.session import Session


async def read_transcript(session: Session, *, target: str, last_n: int = 5) -> dict:
    """Read the transcript of a participant, returning full unclipped text.

    The agent-facing await_sessions reply drops prompt and result text.
    This method returns the full assistant responses from the transcript on
    disk, so a caller that needs the complete text can get it.

    ``target`` accepts a participant id or a current live name directly; do
    not call ``list_participants`` first when the live name is known. Dead
    names are cleared and recyclable, so reading a dead participant requires
    its stable id while the retained row exists.

    Returns the last `last_n` events (user, assistant, tool_call,
    tool_result) from the transcript, in chronological order. Each entry
    has `role`, `text` (full, unclipped), `tool_name`, and `turn_end`.
    """
    if not session._resolved:
        await session.identify()
    record = await session.client.call("read_transcript", id=target, last_n=last_n)
    assert isinstance(record, dict)
    return record
