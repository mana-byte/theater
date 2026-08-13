"""Presentation helpers shared by the CLI and the régie.

Both surfaces show the same three things — the participant tree, a row per
participant, and the bus feed — and both had drifted their own copies of the
tier marks, the home-directory abbreviation, the event summary and the tree
walk. They live here once.

Deliberately free of `rich` and `textual`: the CLI must keep working when the
TUI's dependencies are not importable. Colour and styling stay in
`theater.regie`, which is the only place that has a notion of a theme.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

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
    """A participant may report any harness name it likes.

    Clipped rather than merely padded: one long name must not shear every
    column after it.
    """
    return (harness or "-")[:width]


def event_stamp(ts: float | None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts or 0))


def event_summary(payload: dict | None) -> str:
    """One line describing a bus event, whatever kind it is.

    The bus carries both agent activity and registry bookkeeping, so this
    prefers the fields the observer writes and falls back to raw JSON rather
    than dropping information it does not recognise.
    """
    if not payload:
        return ""
    bits = []
    if payload.get("tool"):
        bits.append(f"[{payload['tool']}]")
    if payload.get("text"):
        bits.append(" ".join(str(payload["text"]).split()))
    if not bits:
        known = {"ts", "turn_end", "index"}
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


_Line = TypeVar("_Line")


def flatten_tree[Line](
    nodes: list[dict],
    render: Callable[[dict, int], _Line],
    indent: int = 0,
) -> list[_Line]:
    """Depth-first walk of `participants.tree`, one rendered line per node.

    The caller supplies the rendering, so the CLI gets plain strings and the
    régie gets Rich Text out of the same traversal.
    """
    out: list[_Line] = []
    for node in nodes:
        out.append(render(node, indent))
        out += flatten_tree(node.get("children", []), render, indent + 1)
    return out
