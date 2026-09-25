"""Presentation helpers shared by the CLI and the régie, so their copies stop drifting.

Free of ``rich``/``textual`` so the CLI works without TUI dependencies; styling stays in Régie.
"""

from __future__ import annotations

import json
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path

#: How each tier is marked in the T column.
TIER_MARK = {"spawned": "S", "adopted": "A", "external": "E"}

#: The legend under any listing that uses TIER_MARK.
TIER_LEGEND = "T: S spawned  A adopted  E external   * not addressable"


def tier_mark(tier: str | None) -> str:
    return TIER_MARK.get(tier or "", "?")


def reach_mark(addressable: object) -> str:
    """A star means the participant can emit but cannot be sent to."""
    return " " if addressable else "*"


def tilde(path: str | None) -> str:
    """Abbreviate the user's home directory, so cwds fit on one line."""
    if not path:
        return "-"
    home = str(Path.home())
    return "~" + path[len(home) :] if path.startswith(home) else path


def short_id(participant_id: str | None) -> str:
    """First 8 chars is enough to tell participants apart on screen."""
    return (participant_id or "????????")[:8]


def clip_harness(harness: str | None, width: int = 11) -> str:
    """Clip a participant-reported harness name so one long name cannot shear later columns."""
    return (harness or "-")[:width]


def presence_suffix(presence: dict | None) -> str:
    """Mark present or unknown focus separately from participant activity."""
    if not presence or not presence.get("protected"):
        return ""
    return "  ◌ human?" if presence.get("state") == "unknown" else "  ◉ human"


def clip_name(name: str | None, width: int = 12) -> str:
    """Clip a (up to 24-char) name to the narrower column so it cannot shear later columns."""
    return (name or "-")[:width]


def display_width(text: str) -> int:
    """Conservative estimate of the terminal cell width of *text*: W/F are two, Mn/Me zero.

    Cosmetic only; Ambiguous-width icons (``◇``, ``▤``) misalign under CJK locales, accepted.
    """
    width = 0
    for ch in text:
        if unicodedata.category(ch) in ("Mn", "Me"):
            continue
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            width += 2
        else:
            width += 1
    return width


def pad_to_width(text: str, column: int) -> str:
    """Left-justify *text* to *column* cells by display width, not codepoints (combining icons)."""
    cells = display_width(text)
    if cells >= column:
        return text
    return text + " " * (column - cells)


def event_stamp(ts: float | None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts or 0))


def event_summary(payload: dict | None) -> str:
    """One line describing a bus event, falling back to raw JSON rather than dropping unknown
    fields.
    """
    if not payload:
        return ""
    bits = []
    if payload.get("tool"):
        bits.append(f"[{payload['tool']}]")
    if payload.get("text"):
        bits.append(" ".join(str(payload["text"]).split()))
    if not bits:
        known = {"ts", "turn_end", "index", "observed_at"}
        rest = {k: v for k, v in payload.items() if k not in known and v is not None}
        if rest:
            bits.append(json.dumps(rest, separators=(",", ":")))
    if payload.get("turn_end"):
        bits.append("(turn end)")
    return " ".join(bits)


def event_who(row: dict) -> str:
    """The from → to pair of a bus row, as one field."""
    who = row.get("from_id") or "-"
    return f"{who} -> {row['to_id']}" if row.get("to_id") else who


def flatten_tree[Line](
    nodes: list[dict],
    render: Callable[[dict, int], Line],
    indent: int = 0,
) -> list[Line]:
    """Depth-first walk of ``participants.tree``; the caller renders, so CLI and régie share it."""
    out: list[Line] = []
    for node in nodes:
        out.append(render(node, indent))
        out += flatten_tree(node.get("children", []), render, indent + 1)
    return out
