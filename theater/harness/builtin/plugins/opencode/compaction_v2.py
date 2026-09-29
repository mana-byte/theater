"""Billed OpenCode 2.x compaction rows — completed or failed — as the usage the path accounts.

A compaction is a paid summarization request, not a conversation step: its row carries the
call's own `tokens`/`cost` and a summary no turn ever sees (session/compaction.ts). It
projects onto the assistant usage shape the observer already accounts — never a turn
boundary, and never a row still running.
"""

from __future__ import annotations

from .constants import OPENCODE_MODEL_ID_KEY, OPENCODE_PROVIDER_ID_KEY
from .values import _table


def message_info(message_id: str, session_id: str, data: dict) -> dict:
    """A compaction row as 1.x-shaped info; only a billed call carries usage."""
    time_data = _table(data.get("time"))
    info: dict = {
        "id": message_id,
        "sessionID": session_id,
        "time": {"created": time_data.get("created")},
    }
    # A failed call bills too (CompactionFailed spreads usage, message-updater.ts); its
    # `error` is dropped here so it never reads as a turn or error boundary.
    if data.get("status") not in ("completed", "failed") or not isinstance(
        data.get("tokens"), dict
    ):
        return {**info, "role": "idle"}
    # A continuation finish keeps the native loop reading: a compaction is never a turn end.
    info = {**info, "role": "assistant", "finish": "unknown"}
    model = _table(data.get("model"))
    if isinstance(model.get("providerID"), str):
        info[OPENCODE_PROVIDER_ID_KEY] = model["providerID"]
    if isinstance(model.get("id"), str):
        info[OPENCODE_MODEL_ID_KEY] = model["id"]
    for key in ("tokens", "cost"):
        if key in data:
            info[key] = data[key]
    return info


__all__ = ["message_info"]
