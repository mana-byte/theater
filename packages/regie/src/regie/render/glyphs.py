"""Glyph composition: spinner, working-harness pulse, status, id, rails, overlays.

Assembles the three rows of Content for one participant leaf and provides the
overlay mechanism that the send animation uses to replace single characters.
"""

from __future__ import annotations

# ruff: noqa: I001
from collections.abc import Mapping

from textual.content import Content

from regie.ui_constants import (
    REGIE_SEND_TRACE_STYLE as SEND_STYLE,
    REGIE_TREE_BRANCH as BRANCH,
    REGIE_TREE_LAST_BRANCH as LAST_BRANCH,
    REGIE_TREE_RAIL as RAIL,
)
from regie.formatting import harness_icon, short_id, tilde
from regie.animations.pulse import working_harness_style
from regie.animations.spinner import spinner_frame

#: An overlay glyph may use the default send style, or carry its own style.
type OverlayGlyph = str | tuple[str, str]

#: A cell within one three-row leaf, used for local overlays.
type LeafCell = tuple[int, int]


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

    Idle uses the harness's own icon so the glyph does double duty; there is no
    separate harness-glyph column.
    """
    status = node.get("status", "?")
    if status == "working":
        return spinner_frame(frame), "$primary"
    if status == "awaiting_input":
        return "!", "$warning"
    if status == "dead":
        return "✗", "$error"
    if status == "idle":
        icon = node.get("icon")
        return (icon if isinstance(icon, str) and icon else harness_icon(node.get("harness"))), (
            "$text-muted"
        )
    # Unknown / unmanaged: honest "?" rather than guessing idle.
    return "?", "$text-muted"


def _presence_glyph_style(node: dict, default: str) -> str:
    """Color the existing harness/status glyph only for confirmed presence."""
    presence = node.get("human_presence") or {}
    return "$accent" if presence.get("state") == "present" else default


def _id_style(node: dict) -> str:
    """A dim-italic id means the participant cannot be sent to.

    Replaces the old ``*`` reach mark: zero columns, and a greyed row reads right
    even to someone who does not know the convention.
    """
    return "$text dim italic" if not node.get("addressable", True) else ""


def _rail_above(prefix: str) -> str:
    """The rail for row 1: the line that leads down into this node's branch.

    A last child gets a rail too (``└`` closes a line from above); only row 3 depends
    on last-ness. The first root's rail is suppressed in :func:`node_label`.
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

    Split parts keep neighbouring styles. Columns past the row end are padded to,
    since a trace crossing a spacer cell would otherwise read as a skip.
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
    reveal: int | None = None,
    detail: str | None = None,
) -> Content:
    """Three rows of Content for one participant leaf.

    Row 1 is a leading spacer carrying the parent rail (blank for the first root);
    row 3 uses ``cont_prefix`` so it doesn't look like a new node.
    """
    # Function-level imports avoid layout ↔ glyphs and reveal ↔ glyphs cycles.
    from regie.animations.reveal import clip_parts
    from regie.render.layout import shorten_path

    glyph, glyph_style = _status_glyph(node, frame)
    glyph_style = _presence_glyph_style(node, glyph_style)
    # Unmanaged panes stuff a tmux pane id into "id" with no name, so fall back to short id.
    sid = node.get("name") or short_id(node.get("id"))
    id_style = _id_style(node)
    cwd = shorten_path(tilde(node.get("cwd")), keep=cwd_segments) if detail is None else detail
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

    if reveal is not None:
        row1_parts = clip_parts(row1_parts, reveal)
        row2_parts = clip_parts(row2_parts, reveal)
        row3_parts = clip_parts(row3_parts, reveal)

    return Content.assemble(
        *row1_parts,
        "\n",
        *row2_parts,
        "\n",
        *row3_parts,
    )
