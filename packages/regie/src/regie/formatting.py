"""Small presentation helpers that deliberately keep public values structured."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from theater.frontend import Participant


def participant_label(participant: Participant) -> str:
    """Use a mutable display name only as decoration, never as an action key."""
    return participant.name or short_identifier(participant.participant_id)


def short_identifier(value: str, *, limit: int = 12) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: max(1, limit - 1)]}…"


def short_id(value: str | None) -> str:
    """Keep legacy rich-tree labels compact without using an action identity."""
    return (value or "????????")[:8]


def tilde(path: str | None) -> str:
    if not path:
        return "-"
    home = str(Path.home())
    return "~" + path[len(home) :] if path.startswith(home) else path


def harness_icon(name: str | None) -> str:
    """Fallback icon for historical tree rows lacking a public catalog entry."""
    return {
        "claude": "✻",
        "codex": "◉",
        "opencode": "◇",
        "pi": "π",
        "vibe": "▤",
    }.get(name or "", "·")


def event_stamp(timestamp: float | None) -> str:
    if timestamp is None:
        return "--:--:--"
    return datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")


def event_summary(payload: object) -> str:
    """Render a bounded diagnostic payload without depending on Theater internals."""
    if not isinstance(payload, Mapping) or not payload:
        return ""
    bits: list[str] = []
    tool = payload.get("tool")
    if tool:
        bits.append(f"[{tool}]")
    text = payload.get("text")
    if text:
        bits.append(" ".join(str(text).split()))
    if not bits:
        omitted = {"ts", "turn_end", "index", "observed_at"}
        rest = {
            key: value for key, value in payload.items() if key not in omitted and value is not None
        }
        if rest:
            bits.append(json.dumps(rest, separators=(",", ":"), default=str))
    if payload.get("turn_end"):
        bits.append("(turn end)")
    return " ".join(bits)


def event_who(row: Mapping[str, object]) -> str:
    """Render diagnostic routing as decoration, never as an action identity."""
    source = row.get("from_id") or "-"
    target = row.get("to_id")
    return f"{source} -> {target}" if target else str(source)


def shorten_path(path: str | None, *, segments: int) -> str:
    if not path:
        return ""
    pieces = [piece for piece in path.split("/") if piece]
    if len(pieces) <= segments:
        return path
    return "…/" + "/".join(pieces[-segments:])


def diagnostic_line(row: Mapping[str, object]) -> str:
    """Render unknown diagnostic rows safely without inventing semantics."""
    kind = row.get("kind")
    identifier = row.get("id")
    label = kind if isinstance(kind, str) else "event"
    suffix = f" #{identifier}" if isinstance(identifier, int) else ""
    return f"{label}{suffix}"


__all__ = [
    "diagnostic_line",
    "event_stamp",
    "event_summary",
    "event_who",
    "harness_icon",
    "participant_label",
    "short_id",
    "short_identifier",
    "shorten_path",
    "tilde",
]
