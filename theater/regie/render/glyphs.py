"""Glyph composition: spinner, working-harness pulse, status, id, rails, overlays.

Assembles the three rows of Content for one participant leaf and provides the
overlay mechanism that the send animation uses to replace single characters.
"""

from __future__ import annotations

# ruff: noqa: I001
from collections.abc import Mapping

from textual.content import Content

from theater.constants.regie import (
    REGIE_SEND_TRACE_STYLE as SEND_STYLE,
    REGIE_SPINNER_FRAMES as SPINNER_FRAMES,
    REGIE_TREE_BRANCH as BRANCH,
    REGIE_TREE_LAST_BRANCH as LAST_BRANCH,
    REGIE_TREE_RAIL as RAIL,
    REGIE_WORKING_HARNESS_STYLES as WORKING_HARNESS_STYLES,
)
from theater.formatting import short_id, tilde
from theater.harness import harness_icon

#: An overlay glyph may use the default send style, or carry its own style.
type OverlayGlyph = str | tuple[str, str]

#: A cell within one three-row leaf, used for local overlays.
type LeafCell = tuple[int, int]


def spinner_frame(frame: int) -> str:
    """The braille character at *frame*, wrapping at 10."""
    return SPINNER_FRAMES[frame % len(SPINNER_FRAMES)]


def working_harness_style(frame: int, offset: int = 0) -> str:
    """The grayscale style for a working harness character at *offset*."""
    return WORKING_HARNESS_STYLES[(offset - frame) % len(WORKING_HARNESS_STYLES)]


def _append_working_harness_text(
    parts: list,
    text: str,
    *,
    frame: int,
    offset: int,
) -> None:
    """Append *text* one styled character at a time."""
    for char in text:
        if char.isspace():
            parts.append(char)
            continue
        parts.append((char, working_harness_style(frame, offset)))
        offset += 1


def _append_working_harness_parts(
    parts: list,
    harness: str,
    sid: str,
    *,
    frame: int,
    id_style: str = "",
) -> None:
    """Append the working harness as a pulse, and the name normally."""
    parts.append(" ")
    _append_working_harness_text(parts, harness, frame=frame, offset=0)
    parts.append("  ")
    if id_style:
        parts.append((sid, id_style))
    else:
        parts.append(sid)


def _status_glyph(node: dict, frame: int = 0) -> tuple[str, str]:
    """The one-character status mark and the theme slot it renders in.

    Returns ``(glyph, style)`` where *style* is a Textual design-token string
    like ``"$primary"``. Idle uses the harness's own icon so the glyph does
    double duty; the separate harness-glyph column is gone.
    """
    status = node.get("status", "?")
    if status == "working":
        return spinner_frame(frame), "$primary"
    if status == "awaiting_input":
        return "!", "$warning"
    if status == "dead":
        return "✗", "$error"
    if status == "idle":
        return harness_icon(node.get("harness")), "$text-muted"
    # Unknown / unmanaged: honest "?" rather than guessing idle.
    return "?", "$text-muted"


def _id_style(node: dict) -> str:
    """A dim-italic id means the participant cannot be sent to.

    The old ``reach_mark`` glyph (``*``) is re-expressed as a style: same
    information, zero columns, and a reader who does not know the convention
    still gets the right impression from a greyed-out row.
    """
    return "$text dim italic" if not node.get("addressable", True) else ""


def _rail_above(prefix: str) -> str:
    """The rail for row 1: the line that leads down into this node's branch.

    Row 2 draws ``├── `` or ``└── `` at this node's own depth, and the line
    arriving there comes down from the parent — so the cell directly above
    the branch glyph is a rail. That holds for a last child too: ``└``
    closes a line that comes from above rather than starting one, so its row
    1 is a rail like everyone else's. Only row 3 turns on last-ness, because
    row 3 is where the line either continues past this node or stops.

    Roots now branch off an invisible super-root, so they have branch
    prefixes and this function computes a rail for them. The first root's
    rail is suppressed in :func:`node_label` via the *is_first_root* flag,
    because nothing visible sits above it and a dangling rail reads as a
    missing row.

    The ancestry to the left is copied through unchanged, gaps and all; only
    this node's own branch column is replaced. Every rail piece is the same
    width, so swapping one for another keeps the columns aligned.
    """
    if not prefix.endswith((BRANCH, LAST_BRANCH)):
        return ""
    return prefix[: -len(BRANCH)] + RAIL


def _overlay_piece(glyph: OverlayGlyph) -> tuple[str, str]:
    """Return the glyph and style for one overlay cell."""
    if isinstance(glyph, tuple):
        return glyph
    return glyph, SEND_STYLE


def _overlay_row(parts: list, overlay: Mapping[int, OverlayGlyph]) -> list:
    """Replace single characters of an assembled row by column.

    *parts* is a ``Content.assemble`` argument list — plain strings and
    ``(text, style)`` pairs — and *overlay* maps a column to the one
    character that should be drawn there instead. The part carrying the
    column is split around it so neighbouring text keeps its own style. A
    column past the end of a row is padded to: the trace sometimes crosses a
    spacer cell, and a packet that disappears there reads as a skip.
    """
    if not overlay:
        return parts
    out: list = []
    col = 0
    for part in parts:
        text = part if isinstance(part, str) else part[0]
        style = "" if isinstance(part, str) else part[1]
        start, col = col, col + len(text)
        hits = sorted(c for c in overlay if start <= c < col)
        if not hits:
            out.append(part)
            continue
        cursor = start
        for hit in hits:
            if hit > cursor:
                chunk = text[cursor - start : hit - start]
                out.append((chunk, style) if style else chunk)
            out.append(_overlay_piece(overlay[hit]))
            cursor = hit + 1
        if cursor < col:
            chunk = text[cursor - start :]
            out.append((chunk, style) if style else chunk)
    for hit in sorted(c for c in overlay if c >= col):
        if hit > col:
            out.append(" " * (hit - col))
        out.append(_overlay_piece(overlay[hit]))
        col = hit + 1
    return out


def node_label(
    node: dict,
    prefix: str = "",
    *,
    cont_prefix: str = "",
    cwd_segments: int = 2,
    frame: int = 0,
    is_first_root: bool = False,
    overlay: Mapping[LeafCell, OverlayGlyph] | None = None,
) -> Content:
    """Three rows of Content for one participant leaf.

    *overlay* maps ``(row_within_the_leaf, column)`` cells to the heavy line
    glyph drawn there — the send animation's travelling trace. It defaults to
    None, so every existing call site renders exactly as before.

    Row 1 is the spacing row — leading rather than trailing, so the first
    leaf gets breathing room under the panel border for free, and the row
    cannot be landed on by a cursor or miscounted by a test. For a child it
    is not empty: it carries the rail arriving from the parent (see
    :func:`_rail_above`), because a blank row there would break the vertical
    line in the gap between every pair of siblings. The first root's row 1
    is also blank: it branches off an invisible super-root, but nothing
    visible sits above it, so the rail is suppressed to avoid a dangling
    line at the top of the panel. Later roots keep the rail because the
    virtual parent connects them to the root above.

    Row 2 carries the *branch* prefix (``├── `` / ``└── ``); row 3 carries
    the *continuation* prefix (``cont_prefix``), which is the rail or gap
    that follows the branch at this depth. Using the branch prefix on row 3
    would make it look like a second node starts there.

    ``Content.assemble`` is used rather than line-by-line ``append`` because
    ``Content.append`` returns a new object rather than mutating in place.
    """
    # Function-level import avoids a layout ↔ glyphs import cycle.
    from theater.regie.render.layout import shorten_path

    glyph, glyph_style = _status_glyph(node, frame)
    # Unmanaged panes stuff a tmux pane id into "id" with no name, so fall back to short id.
    sid = node.get("name") or short_id(node.get("id"))
    id_style = _id_style(node)
    cwd = shorten_path(tilde(node.get("cwd")), keep=cwd_segments)
    harness = node.get("harness", "?")
    harness_pulse = node.get("status") == "working"

    # Row 1: the rail leading into this branch; suppressed for the first root (nothing above it).
    row1_parts: list = []
    if not is_first_root:
        lead = _rail_above(prefix)
        if lead:
            row1_parts.append((lead, "$text dim"))

    # Row 2: rails, glyph, harness, short id; the id is split out so dim-italic applies to it only.
    row2_parts: list = []
    if prefix:
        row2_parts.append((prefix, "$text dim"))
    row2_parts.append((glyph, glyph_style))
    if harness_pulse:
        _append_working_harness_parts(row2_parts, harness, sid, frame=frame, id_style=id_style)
    elif id_style:
        row2_parts.append(f" {harness}  ")
        row2_parts.append((sid, id_style))
    else:
        row2_parts.append(f" {harness}  ")
        row2_parts.append(sid)

    # Row 3: continuation rails (not the branch prefix), shortened cwd, dim.
    row3_parts: list = []
    if cont_prefix:
        row3_parts.append((cont_prefix, "$text dim"))
    row3_parts.append((cwd, "$text dim"))

    if overlay:
        row1_parts = _overlay_row(row1_parts, {c: g for (r, c), g in overlay.items() if r == 0})
        row2_parts = _overlay_row(row2_parts, {c: g for (r, c), g in overlay.items() if r == 1})
        row3_parts = _overlay_row(row3_parts, {c: g for (r, c), g in overlay.items() if r == 2})

    return Content.assemble(
        *row1_parts,
        "\n",
        *row2_parts,
        "\n",
        *row3_parts,
    )
