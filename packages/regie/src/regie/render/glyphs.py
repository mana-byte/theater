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
    REGIE_NAME_MIN_CELLS as NAME_MIN_CELLS,
    REGIE_TREE_COST_MIN_CELLS as COST_MIN_CELLS,
    REGIE_SEND_TRACE_STYLE as SEND_STYLE,
    REGIE_WAITING_FRAMES as WAITING_FRAMES,
    REGIE_WAITING_STEP_FRAMES as WAITING_STEP_FRAMES,
    REGIE_TREE_BRANCH as BRANCH,
    REGIE_TREE_LAST_BRANCH as LAST_BRANCH,
    REGIE_TREE_RAIL as RAIL,
    REGIE_TREE_SEPARATOR_STYLE as SEPARATOR_STYLE,
)
from regie.formatting import harness_icon, short_id, tilde
from regie.marks import RowMark
from regie.motions.marquee import clip_cells
from regie.motions.pulse import working_harness_style
from regie.motions.spinner import spinner_frame
from regie.tree_nodes import RawRenderNode, RenderNode

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
    row1: list = [] if is_first_root else [(_rail_above(prefix), "$text dim")]
    row2: list = [
        (prefix, "$text dim"),
        *(
            [_status_glyph({"status": status}, frame), " "]
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


def _status_glyph(
    node: RawRenderNode, frame: int = 0, *, waiting: str | None = None
) -> tuple[str, str]:
    """The one-character status mark and the theme slot it renders in.

    Idle uses the harness's own icon so the glyph does double duty; there is no
    separate harness-glyph column. An agent blocked on a peer shows a slow clock quadrant
    in its await's colour instead: it is not computing, so it must not look like it is.
    """
    status = node.get("status", "?")
    if (mark := _mark(node)) is not None and mark.replaces_status:
        return mark.glyph, mark.style
    if waiting is not None and status in ("working", "idle"):
        return WAITING_FRAMES[frame // WAITING_STEP_FRAMES % len(WAITING_FRAMES)], waiting
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


def _mark(node: RawRenderNode) -> RowMark | None:
    """What an operation aimed at this row says about it, if anything."""
    mark = node.get("mark")
    return mark if isinstance(mark, RowMark) else None


def _presence_glyph_style(node: RawRenderNode, default: str) -> str:
    """Color the existing harness/status glyph only for confirmed presence."""
    presence = node.get("human_presence") or {}
    return "$accent" if presence.get("state") == "present" else default


def _id_style(node: RawRenderNode) -> str:
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


def shown_name(node: RenderNode | dict) -> str:
    """The row-2 name text: the live alias, or the short id when the row has none.

    Unmanaged panes stuff a tmux pane id into "id" with no name, so they fall back to short id.
    """
    view = cast(RawRenderNode, node)
    return view.get("name") or short_id(view.get("id"))


def harness_shown(node: RenderNode | dict, *, focused: bool) -> bool:
    """Whether row 2 spells out the harness: on the focused row, and wherever the row has no
    name of its own (its id says little, the harness a lot)."""
    return focused or not cast(RawRenderNode, node).get("name")


def _append_pulsing_name(parts: list, name: str, id_style: str, *, frame: int) -> None:
    """The name with the working pulse running along it; an unreachable id keeps its italic."""
    italic = "italic " if "italic" in id_style else ""
    for offset, char in enumerate(name):
        parts.append((char, f"{italic}{working_harness_style(frame, offset)}"))


def _row2_lead(
    node: RawRenderNode,
    prefix: str,
    *,
    frame: int = 0,
    waiting: str | None = None,
    show_harness: bool = True,
    awaited: bool = False,
) -> list:
    """Row-2 parts before the name: prefix rail, status glyph, harness text (when shown).

    An agent peers are waiting on wears ``▏`` in the connector's spacer cell, a thin bar on the
    cell's left edge: the cell is blank otherwise, so it costs no width.
    """
    glyph, glyph_style = _status_glyph(node, frame, waiting=waiting)
    glyph_style = _presence_glyph_style(node, glyph_style)
    harness = node.get("harness", "?")
    parts: list = []
    if prefix and awaited and prefix.endswith(" "):
        parts.extend([(prefix[:-1], "$text dim"), ("▏", "$text")])
    elif prefix:
        parts.append((prefix, "$text dim"))
    parts.append((glyph, glyph_style))
    if not show_harness:
        parts.append("  ")
    elif node.get("status") == "working" and waiting is None:
        parts.append(" ")
        _append_working_harness_text(parts, harness, frame=frame, offset=0)
        parts.append("  ")
    else:
        parts.append(f" {harness}  ")
    return parts


def _fold_variants(view: RawRenderNode, frame: int, *, hint: bool) -> list[list]:
    """The fold badge from fullest to barest: ``▸5 !1 ⠹2``, then ``▸5 !``, then ``▸5``.

    A folded row says how many it hides and how many of them are dead, waiting for you or
    working, worst first, so a branch closed over trouble still shows it. An open row shows ``▾``
    only when *hint* asks, on the row in focus: no chevrons until you look at a row.
    """
    count = view.get("hidden_count")
    if not count:
        return [[("  ▾", "$text-muted")]] if hint and view.get("children") else []
    total = (f"  ▸{count}", "$text-muted")
    glyphs = {
        state: _status_glyph({"status": state}, frame)
        for state in (view.get("hidden_states") or {})
    }
    full = [total] + [
        (f" {glyphs[state][0]}{n}", glyphs[state][1])
        for state, n in (view.get("hidden_states") or {}).items()
    ]
    variants = [full]
    if glyphs:
        worst = next(iter(glyphs.values()))
        variants.append([total, (f" {worst[0]}", worst[1])])
        variants.append([total])
    return [v for i, v in enumerate(variants) if v not in variants[:i]]


def _mark_parts(view: RawRenderNode) -> list:
    mark = _mark(view)
    if mark is not None and not mark.replaces_status:
        return [(f"  {mark.glyph}", mark.style)]
    return []


def fold_parts(
    view: RawRenderNode,
    prefix: str = "",
    *,
    width: int | None = None,
    show_harness: bool = True,
    frame: int = 0,
    hint: bool = False,
) -> list:
    """The fold badge as it fits: the fullest form that still leaves the name its room.

    The name keeps at least ``REGIE_NAME_MIN_CELLS`` (or its whole length when shorter); the
    badge gives way before that, and never disappears: its arrow is the click target.
    """
    variants = _fold_variants(view, frame, hint=hint)
    if not variants or width is None or len(variants) == 1:
        return variants[0] if variants else []
    lead = _parts_width(_row2_lead(view, prefix, show_harness=show_harness))
    marks = _parts_width(_mark_parts(view))
    need = min(cell_len(shown_name(view)), NAME_MIN_CELLS)
    for variant in variants:
        if width - lead - marks - _parts_width(variant) >= need:
            return variant
    return variants[-1]


def name_room(
    node: RenderNode | dict,
    prefix: str,
    *,
    width: int | None,
    show_harness: bool,
    fold_hint: bool = False,
    frame: int = 0,
) -> int | None:
    """Cells row two leaves for the name once the glyph, harness and badges have theirs.

    None while the row has no width yet.
    """
    if width is None:
        return None
    view = cast(RawRenderNode, node)
    lead = _parts_width(_row2_lead(view, prefix, show_harness=show_harness))
    fold = fold_parts(
        view, prefix, width=width, show_harness=show_harness, frame=frame, hint=fold_hint
    )
    return max(1, width - lead - _parts_width(fold) - _parts_width(_mark_parts(view)))


def fit_name(name: str, room: int) -> str:
    """*name* as it fits in *room* cells: a name too long ends in ``…`` instead of wrapping."""
    if cell_len(name) <= room:
        return name
    return clip_cells(name, room - 1) + "…" if room > 1 else "…"


def visible_name_span(
    node: RenderNode | dict,
    prefix: str = "",
    *,
    reveal: int | None = None,
    show_harness: bool = True,
    name_text: str | None = None,
) -> tuple[int, int] | None:
    """Row-2 cell span of the name after the renderer's reveal clipping; None while hidden.

    Reveal counts codepoints while columns are cells, so clip then measure. *name_text* is the
    name as drawn when that is not the whole name (clipped to the room, or scrolled).
    """
    from regie.motions.reveal import clip_parts

    lead = _row2_lead(cast(RawRenderNode, node), prefix, show_harness=show_harness)
    name = shown_name(node) if name_text is None else name_text
    lead_pts = sum(len(part if isinstance(part, str) else part[0]) for part in lead)
    start = _parts_width(lead)
    if reveal is None:
        return start, start + cell_len(name)
    shown = clip_parts([name], reveal - lead_pts)
    if not shown:
        return None
    return start, start + _parts_width(shown)


def _right_aligned(
    parts: list, tail: Sequence[str | tuple[str, str]] | None, width: int | None
) -> list:
    """*parts* with *tail* pushed to the right edge, when the row has room for both."""
    if tail and width is not None:
        gap = width - _parts_width(parts) - _parts_width(tail)
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
    mark = _mark(view)
    if mark is not None and mark.detail:
        reason = mark.detail
        if width is not None:  # a long reason must neither wrap nor push the cost out
            reserve = max(_parts_width(cost or ()), COST_MIN_CELLS) + 1 if cost else 0
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
    reveal: int | None = None,
    detail: str | None = None,
    cost: Sequence[str | tuple[str, str]] | None = None,
    hint: Sequence[str | tuple[str, str]] | None = None,
    width: int | None = None,
    waiting: str | None = None,
    awaited: int = 0,
    show_harness: bool = True,
    name_text: str | None = None,
    fold_hint: bool = False,
) -> Content:
    """Three rows of Content for one participant leaf.

    Row 1 is a leading spacer carrying the parent rail (blank for the first root);
    row 3 uses ``cont_prefix`` so it doesn't look like a new node. *waiting* is the colour of
    the await this agent is blocked in; *awaited* is how many peers are blocked on it (the row
    wears ``▏`` in its connector whatever the number). Without *show_harness* the working pulse
    runs along the name instead of the harness text. A name wider than the room ends in ``…`` so
    row two never wraps over the directory; *name_text* replaces it with the caller's own window
    of the name (scrolling, or blanks under an editor).
    """
    # Function-level imports avoid layout ↔ glyphs and reveal ↔ glyphs cycles.
    from regie.motions.reveal import clip_parts
    from regie.render.layout import shorten_path

    view = cast(RawRenderNode, node)
    sid = shown_name(view)
    if name_text is not None:
        sid = name_text
    elif (
        room := name_room(
            view,
            prefix,
            width=width,
            show_harness=show_harness,
            fold_hint=fold_hint,
            frame=frame,
        )
    ) is not None:
        sid = fit_name(sid, room)
    id_style = _id_style(view)
    cwd = shorten_path(tilde(view.get("cwd")), keep=cwd_segments) if detail is None else detail

    # Row 1: the rail leading into this branch; suppressed for the first root (nothing above it).
    row1_parts: list = []
    if not is_first_root:
        lead = _rail_above(prefix)
        if lead:
            row1_parts.append((lead, "$text dim"))

    # Row 2: rails, glyph, harness, short id; the id is split out so dim-italic applies to it only.
    row2_parts: list = _row2_lead(
        view, prefix, frame=frame, waiting=waiting, show_harness=show_harness, awaited=bool(awaited)
    )
    if view.get("status") == "working" and waiting is None and not show_harness:
        _append_pulsing_name(row2_parts, sid, id_style, frame=frame)
    else:
        row2_parts.append((sid, id_style) if id_style else sid)
    row2_parts.extend(
        fold_parts(
            view, prefix, width=width, show_harness=show_harness, frame=frame, hint=fold_hint
        )
    )
    row2_parts.extend(_mark_parts(view))
    if width is not None and _parts_width(row2_parts) > width:
        row2_parts = clip_parts(row2_parts, width)  # too deep or too narrow even for "…": crop
    row2_parts = _right_aligned(row2_parts, hint, width)
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
