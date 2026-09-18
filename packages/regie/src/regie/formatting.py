"""Small presentation helpers that deliberately keep public values structured."""

from __future__ import annotations

from collections.abc import Mapping

from theater.frontend import Participant


def participant_label(participant: Participant) -> str:
    """Use a mutable display name only as decoration, never as an action key."""
    return participant.name or short_identifier(participant.participant_id)


def short_identifier(value: str, *, limit: int = 12) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: max(1, limit - 1)]}…"


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


__all__ = ["diagnostic_line", "participant_label", "short_identifier", "shorten_path"]
