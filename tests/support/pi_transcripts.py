"""Builders for Pi session transcripts and fork markers shared by the Pi tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from theater.harness.builtin.plugins.pi.constants import (
    PI_SWITCH_MARKER,
    PI_SWITCH_MARKER_VERSION,
    PI_SWITCHES_DIRNAME,
)


def session_record(
    *,
    session_id: str,
    cwd: Path,
    timestamp: str = "2026-08-31T12:00:00.000Z",
    parent_session: Path | None = None,
) -> dict[str, object]:
    session: dict[str, object] = {
        "type": "session",
        "version": 3,
        "id": session_id,
        "timestamp": timestamp,
        "cwd": str(cwd),
    }
    if parent_session is not None:
        session["parentSession"] = str(parent_session.resolve())
    return session


def message_record(entry_id: str, message: dict[str, object]) -> dict[str, object]:
    return {
        "type": "message",
        "id": entry_id,
        "timestamp": "2026-08-31T12:00:01.000Z",
        "message": message,
    }


def bash_execution(
    entry_id: str = "bash-1", *, exit_code: int | None = 0, cancelled: bool = False
) -> dict[str, object]:
    return message_record(
        entry_id,
        {
            "role": "bashExecution",
            "command": "git push",
            "output": "done",
            "exitCode": exit_code,
            "cancelled": cancelled,
            "truncated": False,
            "timestamp": 1788349677604,
        },
    )


def append(path: Path, *records: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record) + "\n")


def mark_switch(
    root: Path,
    *,
    previous: Path,
    target: Path,
    reason: str,
    offset: int | None = None,
    records: int | None = None,
) -> None:
    if reason == "new":
        offset = records = 0
        dev = ino = None
    elif target.exists():
        stat = target.stat()
        offset = stat.st_size if offset is None else offset
        records = None
        dev, ino = stat.st_dev, stat.st_ino
    else:
        dev = ino = None
    write_switch_marker(
        root,
        target,
        {
            "version": PI_SWITCH_MARKER_VERSION,
            "reason": reason,
            "location": str(target.resolve()),
            "previous_location": str(previous.resolve()),
            "offset": offset,
            "records": records,
            "dev": dev,
            "ino": ino,
        },
    )


def write_switch_marker(root: Path, target: Path, value: dict[str, object]) -> None:
    body = json.dumps(value) + "\n"
    (root / PI_SWITCH_MARKER).write_text(body, encoding="utf-8")
    archive = root / PI_SWITCHES_DIRNAME
    archive.mkdir(exist_ok=True)
    digest = hashlib.sha256(str(target.resolve()).encode()).hexdigest()
    (archive / f"{digest}.json").write_text(body, encoding="utf-8")
