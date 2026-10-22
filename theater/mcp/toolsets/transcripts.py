"""Bounded transcript reading for agents."""

from __future__ import annotations

from theater.mcp.session import Session


def _event_summary(event: dict) -> dict:
    """Complete events need no reconstruction coordinates; partial ones still do."""
    summary = {"role": event["role"]}
    for key in ("text", "tool_name", "turn_end"):
        if event.get(key):
            summary[key] = event[key]
    if event.get("turn_terminal") is not None:
        summary["turn_terminal"] = event["turn_terminal"]
    partial = (
        event.get("text_start_byte", 0) != 0
        or event.get("text_end_byte") != event.get("text_total_bytes")
        or "middle_cursor" in event
    )
    if partial:
        for key in ("index", "text_start_byte", "text_end_byte", "text_total_bytes"):
            summary[key] = event[key]
    for key in ("tail", "elided_bytes", "middle_cursor"):
        if key in event:
            summary[key] = event[key]
    return summary


async def read_transcript(
    session: Session,
    *,
    target: str,
    cursor: str | None = None,
    roles: list[str] | None = None,
) -> dict:
    """Request and project one bounded transcript page."""
    await session.caller_id()
    params: dict[str, object] = {"id": target}
    if cursor is not None:
        params["cursor"] = cursor
    if roles is not None:
        params["roles"] = roles
    record = await session.client.call("read_transcript", **params)
    assert isinstance(record, dict)
    record = dict(record)
    record.pop("cursor", None)
    record.pop("has_more", None)
    events = record["events"]
    sealed = {
        event["repeats_event_position"] for event in events if "repeats_event_position" in event
    }
    record["events"] = [
        _event_summary(event) for event in events if event.get("event_position") not in sealed
    ]
    if roles is not None:
        for key in ("first_prompt", "later_prompts", "resumed_from"):
            record.pop(key, None)
    elif record.get("first_prompt") and any(
        event.get("text") == record["first_prompt"] for event in record["events"]
    ):
        record.pop("first_prompt")
    return record
