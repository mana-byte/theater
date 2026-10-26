"""Glyph composition: spinner, working-harness pulse, status, id, rails, overlays.

Assembles the three rows of Content for one participant leaf and provides the
overlay mechanism that the send animation uses to replace single characters.
"""

from __future__ import annotations

# ruff: noqa: I001
from collections.abc import Mapping, Sequence
from typing import cast

from rich.cells import cell_len
from textual.content import Content

from regie.ui_constants import (
    REGIE_TREE_COST_MIN_CELLS as COST_MIN_CELLS,
    REGIE_SEND_TRACE_STYLE as SEND_STYLE,
    REGIE_TREE_BRANCH as BRANCH,
    REGIE_TREE_LAST_BRANCH as LAST_BRANCH,
    REGIE_TREE_RAIL as RAIL,
    REGIE_TREE_SEPARATOR_STYLE as SEPARATOR_STYLE,
)
from regie.formatting import tilde
from regie.motions.pulse import working_harness_style
from regie.tree_nodes import RawRenderNode, RenderNode
from regie.render.row2 import (
    Row2Layout,
    fit_name,
    harness_shown,  # noqa: F401 - retained render API
    name_room,  # noqa: F401 - retained render API
    parts_width,
    row2_layout,
    row_mark,
    shown_name,  # noqa: F401 - retained render API
    status_glyph,
    visible_name_span,  # noqa: F401 - retained render API
)

#: An overlay glyph may use the default send style, or carry its own style.
type OverlayGlyph = str | tuple[str, str]

#: A cell within one three-row leaf, used for local overlays.
type LeafCell = tuple[int, int]


def with_gutter_bar(content: Content, style: str) -> Content:
    """Prefix every row with a ``▌`` bar in *style*, taking the two columns the padding gave up."""
    lines = content.split("\n", allow_blank=True)
    return Content("\n").join(Content.assemble(("▌", style), " ", line) for line in lines)


def with_stage_marker(content: Content, marker: str | None) -> Content:
    """The stage bar: tmux staging in primary, a trajectory in accent."""
    if marker is None:
        return content
    return with_gutter_bar(content, "$primary" if marker == "tmux" else "$accent")


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
    count: int | None = 0,
    collapsed: bool = False,
    status: str | None = None,
    frame: int = 0,
    is_first_root: bool = False,
    overlay: Mapping[LeafCell, OverlayGlyph] | None = None,
) -> Content:
    """A section heading on the tree's own branch: ``├── ▾ BACKEND · 3``, three rows tall.

    *status* is what a folded section's hidden agents are doing; it takes the chevron's place.
    """
    row1: list = [] if is_first_root else [(rail_above(prefix), "$text dim")]
    row2: list = [
        (prefix, "$text dim"),
        *(
            [status_glyph({"status": status}, frame), " "]
            if status is not None
            else [(separator_chevron(collapsed), SEPARATOR_STYLE)]
        ),
        (name.upper(), SEPARATOR_STYLE),
        *([] if count is None else [(f" · {count}", "$text dim")]),
    ]
    row3: list = [(separator_prefix(prefix), "$text dim")]
    rows = [
        _overlay_row(parts, {c: g for (r, c), g in (overlay or {}).items() if r == index})
        for index, parts in enumerate((row1, row2, row3))
    ]
    return Content.assemble(*rows[0], "\n", *rows[1], "\n", *rows[2])


def _id_style(node: RawRenderNode) -> str:
    """A dim-italic id means the participant cannot be sent to.

    Replaces the old ``*`` reach mark: zero columns, and a greyed row reads right
    even to someone who does not know the convention.
    """
    return "$text dim italic" if not node.get("addressable", True) else ""


def rail_above(prefix: str) -> str:
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


def _append_pulsing_name(parts: list, name: str, id_style: str, *, frame: int) -> None:
    """The name with the working pulse running along it; an unreachable id keeps its italic."""
    italic = "italic " if "italic" in id_style else ""
    for offset, char in enumerate(name):
        parts.append((char, f"{italic}{working_harness_style(frame, offset)}"))


def _right_aligned(
    parts: list, tail: Sequence[str | tuple[str, str]] | None, width: int | None
) -> list:
    """*parts* with *tail* pushed to the right edge, when the row has room for both."""
    if tail and width is not None:
        gap = width - parts_width(parts) - parts_width(tail)
        if gap > 0:
            return [*parts, " " * gap, *tail]
    return parts


def _row3_parts(
    view: RawRenderNode,
    cont_prefix: str,
    cwd: str,
    cost: Sequence[str | tuple[str, str]] | None,
    width: int | None,
) -> list:
    """Row three: continuation rails, the directory (or an operation's reason), then the cost."""
    parts: list = [(cont_prefix, "$text dim")] if cont_prefix else []
    mark = row_mark(view)
    if mark is not None and mark.detail:
        reason = mark.detail
        if width is not None:  # a long reason must neither wrap nor push the cost out
            reserve = max(parts_width(cost or ()), COST_MIN_CELLS) + 1 if cost else 0
            reason = fit_name(reason, max(1, width - cell_len(cont_prefix) - reserve))
        parts.append((reason, mark.style))
    else:
        parts.append((cwd, "$text dim"))
    return _right_aligned(parts, cost, width)


def node_label(
    node: RenderNode | dict,
    prefix: str = "",
    *,
    cont_prefix: str = "",
    cwd_segments: int = 2,
    frame: int = 0,
    is_first_root: bool = False,
    overlay: Mapping[LeafCell, OverlayGlyph] | None = None,
    detail: str | None = None,
    cost: Sequence[str | tuple[str, str]] | None = None,
    layout: Row2Layout | None = None,
) -> Content:
    """Three rows of Content for one participant leaf.

    Row 1 is a leading spacer carrying the parent rail (blank for the first root);
    row 3 uses ``cont_prefix`` so it doesn't look like a new node.
    """
    # Function-level imports avoid layout ↔ glyphs and reveal ↔ glyphs cycles.
    from regie.motions.reveal import clip_parts
    from regie.render.layout import shorten_path

    view = cast(RawRenderNode, node)
    layout = layout or row2_layout(view, prefix, frame=frame)
    width, reveal = layout.width, layout.reveal
    sid = layout.name_text
    id_style = _id_style(view)
    cwd = shorten_path(tilde(view.get("cwd")), keep=cwd_segments) if detail is None else detail

    # Row 1: the rail leading into this branch; suppressed for the first root (nothing above it).
    row1_parts: list = []
    if not is_first_root:
        lead = rail_above(prefix)
        if lead:
            row1_parts.append((lead, "$text dim"))

    # Row 2: rails, glyph, harness, short id; the id is split out so dim-italic applies to it only.
    row2_parts: list = list(layout.lead_parts)
    if layout.pulse_name:
        _append_pulsing_name(row2_parts, sid, id_style, frame=frame)
    else:
        row2_parts.append((sid, id_style) if id_style else sid)
    row2_parts.extend(layout.fold_parts)
    row2_parts.extend(layout.mark_parts)
    if width is not None and parts_width(row2_parts) > width:
        row2_parts = clip_parts(row2_parts, width)  # too deep or too narrow even for "…": crop
    row2_parts = _right_aligned(row2_parts, layout.hint_parts, width)
    row3_parts = _row3_parts(view, cont_prefix, cwd, cost, width)

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
