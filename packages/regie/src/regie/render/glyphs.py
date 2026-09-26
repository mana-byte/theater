"""Glyph composition: spinner, working-harness pulse, status, id, rails, overlays.

Assembles the three rows of Content for one participant leaf and provides the
overlay mechanism that the send animation uses to replace single characters.
"""

from __future__ import annotations

# ruff: noqa: I001
from collections.abc import Mapping, Sequence

from rich.cells import cell_len
from textual.content import Content

from regie.ui_constants import (
    REGIE_SEND_TRACE_STYLE as SEND_STYLE,
    REGIE_TREE_BRANCH as BRANCH,
    REGIE_TREE_LAST_BRANCH as LAST_BRANCH,
    REGIE_TREE_RAIL as RAIL,
    REGIE_TREE_SEPARATOR_STYLE as SEPARATOR_STYLE,
)
from regie.formatting import harness_icon, short_id, tilde
from regie.animations.pulse import working_harness_style
from regie.animations.spinner import spinner_frame

#: An overlay glyph may use the default send style, or carry its own style.
type OverlayGlyph = str | tuple[str, str]

#: A cell within one three-row leaf, used for local overlays.
type LeafCell = tuple[int, int]


def separator_chevron(collapsed: bool) -> str:
    return "▸ " if collapsed else "▾ "


def separator_name_span(name: str, prefix: str) -> tuple[int, int]:
    """Row-2 cell span of a separator's name: right after its branch and chevron."""
    start = cell_len(prefix) + cell_len(separator_chevron(False))
    return start, start + cell_len(name.upper())


def separator_prefix(prefix: str) -> str:
    """The rails a separator's last row continues with: its branch becomes a plain rail."""
    if not prefix.endswith((BRANCH, LAST_BRANCH)):
        return prefix
    tail = RAIL if prefix.endswith(BRANCH) else " " * cell_len(BRANCH)
    return prefix[: -len(BRANCH)] + tail.ljust(len(BRANCH))


def separator_label(
    name: str,
    prefix: str,
    *,
    count: int = 0,
    collapsed: bool = False,
    is_first_root: bool = False,
    overlay: Mapping[LeafCell, OverlayGlyph] | None = None,
) -> Content:
    """A section heading on the tree's own branch: ``├── ▾ BACKEND · 3``, three rows tall."""
    row1: list = [] if is_first_root else [(_rail_above(prefix), "$text dim")]
    row2: list = [
        (prefix, "$text dim"),
        (separator_chevron(collapsed), SEPARATOR_STYLE),
        (name.upper(), SEPARATOR_STYLE),
        (f" · {count}", "$text dim"),
    ]
    row3: list = [(separator_prefix(prefix), "$text dim")]
    rows = [
        _overlay_row(parts, {c: g for (r, c), g in (overlay or {}).items() if r == index})
        for index, parts in enumerate((row1, row2, row3))
    ]
    return Content.assemble(*rows[0], "\n", *rows[1], "\n", *rows[2])


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


def _parts_width(parts: Sequence[str | tuple[str, str]]) -> int:
    return sum(cell_len(part if isinstance(part, str) else part[0]) for part in parts)


def shown_name(node: dict) -> str:
    """The row-2 name text: the live alias, or the short id when the row has none.

    Unmanaged panes stuff a tmux pane id into "id" with no name, so they fall back to short id.
    """
    return node.get("name") or short_id(node.get("id"))


def _row2_lead(node: dict, prefix: str, *, frame: int = 0) -> list:
    """Row-2 parts before the name: prefix rail, status glyph, harness text."""
    glyph, glyph_style = _status_glyph(node, frame)
    glyph_style = _presence_glyph_style(node, glyph_style)
    harness = node.get("harness", "?")
    parts: list = []
    if prefix:
        parts.append((prefix, "$text dim"))
    parts.append((glyph, glyph_style))
    if node.get("status") == "working":
        parts.append(" ")
        _append_working_harness_text(parts, harness, frame=frame, offset=0)
        parts.append("  ")
    else:
        parts.append(f" {harness}  ")
    return parts


def visible_name_span(
    node: dict, prefix: str = "", *, reveal: int | None = None
) -> tuple[int, int] | None:
    """Row-2 cell span of the name after the renderer's reveal clipping; None while hidden.

    Reveal counts codepoints while columns are cells, so clip then measure.
    """
    from regie.animations.reveal import clip_parts

    lead = _row2_lead(node, prefix)
    name = shown_name(node)
    lead_pts = sum(len(part if isinstance(part, str) else part[0]) for part in lead)
    start = _parts_width(lead)
    if reveal is None:
        return start, start + cell_len(name)
    shown = clip_parts([name], reveal - lead_pts)
    if not shown:
        return None
    return start, start + _parts_width(shown)


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
    cost: Sequence[str | tuple[str, str]] | None = None,
    width: int | None = None,
) -> Content:
    """Three rows of Content for one participant leaf.

    Row 1 is a leading spacer carrying the parent rail (blank for the first root);
    row 3 uses ``cont_prefix`` so it doesn't look like a new node.
    """
    # Function-level imports avoid layout ↔ glyphs and reveal ↔ glyphs cycles.
    from regie.animations.reveal import clip_parts
    from regie.render.layout import shorten_path

    sid = shown_name(node)
    id_style = _id_style(node)
    cwd = shorten_path(tilde(node.get("cwd")), keep=cwd_segments) if detail is None else detail

    # Row 1: the rail leading into this branch; suppressed for the first root (nothing above it).
    row1_parts: list = []
    if not is_first_root:
        lead = _rail_above(prefix)
        if lead:
            row1_parts.append((lead, "$text dim"))

    # Row 2: rails, glyph, harness, short id; the id is split out so dim-italic applies to it only.
    row2_parts: list = _row2_lead(node, prefix, frame=frame)
    row2_parts.append((sid, id_style) if id_style else sid)
    if cost is not None and width is not None:
        gap = width - _parts_width(row2_parts) - _parts_width(cost)
        if gap > 0:
            row2_parts.extend((" " * gap, *cost))

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
