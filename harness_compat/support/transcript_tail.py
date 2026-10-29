"""The last records of a transcript, short enough to survive a CI report's clipping."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def transcript_tail(location: str | None, *, launched_at: float | None, records: int = 6) -> str:
    """Write order of the last records, each ``type/detail uuid<parent @seconds-after-launch``.

    A harness may write a turn's records out of causal order, and which of them predate the launch
    decides what a reader may call history, so a failure CI cannot rerun needs both.
    """
    if not location or not Path(location).is_file():
        return "no transcript file"
    lines = Path(location).read_text(encoding="utf-8", errors="replace").splitlines()
    tokens = [token for line in lines if (token := _token(line, launched_at)) is not None]
    shown = tokens[-records:]
    return f"{len(lines)} lines, last {len(shown)} records: " + " | ".join(shown)


def _token(line: str, launched_at: float | None) -> str | None:
    """None for a metadata line (no ``uuid``/``id``): not part of the causal chain."""
    try:
        record = json.loads(line)
    except ValueError:
        return "?"
    if not isinstance(record, dict):
        return "?"
    identifier = record.get("uuid", record.get("id"))
    if not identifier:
        return None
    attachment = record.get("attachment")
    detail = attachment.get("type") if isinstance(attachment, dict) else record.get("subtype")
    kind = "/".join(str(part) for part in (record.get("type"), detail) if part)
    parent = record.get("parentUuid", record.get("parentId"))
    token = f"{kind or '?'} {_short(identifier)}<{_short(parent)}"
    written = _epoch(record.get("timestamp"))
    if written is not None and launched_at is not None:
        token += f" @{written - launched_at:+.1f}s"
    return token


def _short(identifier: object) -> str:
    return str(identifier)[:4] if identifier else "-"


def _epoch(stamp: object) -> float | None:
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return None
