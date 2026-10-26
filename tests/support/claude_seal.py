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


def claude_fork(copied_turns: int, *, attachments: bool = False) -> list[str]:
    """A fork's answer/marker, copied turns, then its prompt and optional late attachments."""

    def stamped(line: str, second: int) -> str:
        record = json.loads(line)
        record.update(timestamp=f"2026-01-01T00:00:{second:02d}Z", sessionId="fork")
        return json.dumps(record)

    lines = [
        stamped(claude_record("old_user0", None, "user", message={"content": "old0"}), 0),
        stamped(
            claude_fragment(
                "new_answer",
                "att2" if attachments else "new_user",
                {"type": "text", "text": "NEW"},
                "msg_new",
            ),
            5,
        ),
    ]
    if attachments:
        lines.append(stamped(claude_record("snapshot", "new_answer", "attachment"), 5))
    lines.append(
        stamped(claude_marker("new_marker", "snapshot" if attachments else "new_answer"), 5)
    )
    for number in range(copied_turns):
        if number:
            lines.append(
                stamped(
                    claude_record(
                        f"old_user{number}",
                        f"old_marker{number - 1}",
                        "user",
                        message={"content": f"old{number}"},
                    ),
                    0,
                )
            )
        lines.extend(
            [
                stamped(
                    claude_fragment(
                        f"old_answer{number}",
                        f"old_user{number}",
                        {"type": "text", "text": f"OLD{number}"},
                        f"msg_old{number}",
                    ),
                    0,
                ),
                stamped(claude_marker(f"old_marker{number}", f"old_answer{number}"), 0),
            ]
        )
    lines.append(
        stamped(
            claude_record(
                "new_user",
                f"old_marker{copied_turns - 1}",
                "user",
                message={"content": "new"},
            ),
            4,
        )
    )
    if attachments:
        lines.extend(
            [
                stamped(claude_record("att1", "new_user", "attachment"), 5),
                stamped(claude_record("att2", "att1", "attachment"), 5),
            ]
        )
    return lines
