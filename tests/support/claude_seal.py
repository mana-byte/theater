"""Claude transcript records for the final-answer seal and fragment shapes."""

from __future__ import annotations

import json

VERSION = "2.1.284"


def claude_record(uuid: str, parent: str | None, kind: str = "assistant", **extra) -> str:
    return json.dumps(
        {"type": kind, "uuid": uuid, "parentUuid": parent, "version": VERSION, **extra}
    )


def claude_fragment(uuid: str, parent: str | None, block: dict, message: str = "msg_1") -> str:
    message_body = {"id": message, "stop_reason": "end_turn", "content": [block]}
    return claude_record(uuid, parent, message=message_body)


def claude_marker(uuid: str, parent: str) -> str:
    return claude_record(uuid, parent, "system", subtype="turn_duration")
