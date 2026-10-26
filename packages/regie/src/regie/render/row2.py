"""Row-two participant layout and its visible click geometry."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from rich.cells import cell_len

from regie.formatting import harness_icon, short_id
from regie.marks import RowMark
from regie.motions.marquee import clip_cells, marquee_cells, overflows_cells
from regie.motions.pulse import working_harness_style
from regie.motions.spinner import spinner_frame
from regie.tree_nodes import RawRenderNode, RenderNode
from regie.ui_constants import (
    REGIE_AWAITED_MARK as AWAITED_MARK,
)
from regie.ui_constants import (
    REGIE_NAME_MIN_CELLS as NAME_MIN_CELLS,
)
from regie.ui_constants import (
    REGIE_WAITING_FRAMES as WAITING_FRAMES,
)
from regie.ui_constants import (
    REGIE_WAITING_STEP_FRAMES as WAITING_STEP_FRAMES,
)


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


def status_glyph(
    node: RawRenderNode, frame: int = 0, *, waiting: str | None = None
) -> tuple[str, str]:
    """The one-character status mark and the theme slot it renders in.

    Idle uses the harness's own icon so the glyph does double duty; there is no
    separate harness-glyph column. An agent blocked on a peer shows a slow clock quadrant
    in its await's colour instead: it is not computing, so it must not look like it is.
    """
    status = node.get("status", "?")
    if (mark := row_mark(node)) is not None and mark.replaces_status:
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


def row_mark(node: RawRenderNode) -> RowMark | None:
    """What an operation aimed at this row says about it, if anything."""
    mark = node.get("mark")
    return mark if isinstance(mark, RowMark) else None


def _presence_glyph_style(node: RawRenderNode, default: str) -> str:
    """Color the existing harness/status glyph only for confirmed presence."""
    presence = node.get("human_presence") or {}
    return "$accent" if presence.get("state") == "present" else default


def parts_width(parts: Sequence[str | tuple[str, str]]) -> int:
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


def row2_lead(
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
    cell's left edge drawn like the rails: the cell is blank otherwise, so it costs no width.
    """
    glyph, glyph_style = status_glyph(node, frame, waiting=waiting)
    glyph_style = _presence_glyph_style(node, glyph_style)
    harness = node.get("harness", "?")
    parts: list = []
    if prefix and awaited and prefix.endswith(" "):
        parts.append((prefix[:-1] + AWAITED_MARK, "$text dim"))
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
        state: status_glyph({"status": state}, frame) for state in (view.get("hidden_states") or {})
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


def mark_parts(view: RawRenderNode) -> list:
    mark = row_mark(view)
    if mark is not None and not mark.replaces_status:
        return [(f"  {mark.glyph}", mark.style)]
    return []


def fit_name(name: str, room: int) -> str:
    """Clip a name with an ellipsis when it exceeds the room."""
    if cell_len(name) <= room:
        return name
    return clip_cells(name, room - 1) + "…" if room > 1 else "…"


@dataclass(frozen=True, slots=True)
class Row2Layout:
    lead_parts: tuple[str | tuple[str, str], ...]
    fold_parts: tuple[str | tuple[str, str], ...]
    mark_parts: tuple[str | tuple[str, str], ...]
    hint_parts: tuple[str | tuple[str, str], ...]
    name_room: int | None
    name_text: str
    name_scrolls: bool
    name_span: tuple[int, int] | None
    fold_span: tuple[int, int] | None
    width: int | None
    reveal: int | None
    pulse_name: bool


def row2_layout(
    node: RenderNode | dict,
    prefix: str = "",
    *,
    width: int | None = None,
    frame: int = 0,
    waiting: str | None = None,
    awaited: bool = False,
    show_harness: bool = True,
    fold_hint: bool = False,
    focused: bool = False,
    reveal: int | None = None,
    name_text: str | None = None,
    marquee_offset: int = 0,
    renaming: bool = False,
    hint: Sequence[str | tuple[str, str]] | None = None,
) -> Row2Layout:
    """Compute the drawn name and its row-two geometry together."""
    from regie.motions.reveal import clip_parts

    view = cast(RawRenderNode, node)
    lead = tuple(
        row2_lead(
            view,
            prefix,
            frame=frame,
            waiting=waiting,
            show_harness=show_harness,
            awaited=awaited,
        )
    )
    glyph = lead[1 if prefix else 0]
    drawn_glyph = glyph[0] if isinstance(glyph, tuple) else glyph
    base_glyph = status_glyph(view)[0]
    # The old room and click span used the base icon even while an await clock replaced it.
    sizing_lead = parts_width(lead) + cell_len(base_glyph) - cell_len(drawn_glyph)
    marks = tuple(mark_parts(view))
    variants = _fold_variants(view, frame, hint=fold_hint)
    fold = variants[0] if variants else []
    if width is not None and len(variants) > 1:
        need = min(cell_len(shown_name(view)), NAME_MIN_CELLS)
        for variant in variants:
            if width - sizing_lead - parts_width(marks) - parts_width(variant) >= need:
                fold = variant
                break
        else:
            fold = variants[-1]
    room = (
        None
        if width is None
        else max(1, width - sizing_lead - parts_width(fold) - parts_width(marks))
    )
    raw_name = shown_name(view)
    scrolls = focused and room is not None and overflows_cells(raw_name, room)
    if name_text is not None:
        name = name_text
    elif room is None:
        name = raw_name
    elif renaming:
        name = " " * min(cell_len(raw_name), room)
    elif scrolls:
        name = marquee_cells(raw_name, room, marquee_offset)
    else:
        name = fit_name(raw_name, room)
    lead_points = sum(len(part if isinstance(part, str) else part[0]) for part in lead)
    lead_points += len(base_glyph) - len(drawn_glyph)
    start = sizing_lead
    shown = [name] if reveal is None else clip_parts([name], reveal - lead_points)
    name_span = (start, start + parts_width(shown)) if shown else None
    fold_span = (
        (name_span[1], name_span[1] + parts_width(fold))
        if fold and name_span is not None and not renaming
        else None
    )
    return Row2Layout(
        lead,
        tuple(fold),
        marks,
        tuple(hint or ()),
        room,
        name,
        scrolls,
        name_span,
        fold_span,
        width,
        reveal,
        view.get("status") == "working" and waiting is None and not show_harness,
    )


def name_room(
    node: RenderNode | dict,
    prefix: str,
    *,
    width: int | None,
    show_harness: bool,
    fold_hint: bool = False,
    frame: int = 0,
) -> int | None:
    """Cells row two leaves for the name, or None before the row has a width."""
    return row2_layout(
        node, prefix, width=width, show_harness=show_harness, fold_hint=fold_hint, frame=frame
    ).name_room


def visible_name_span(
    node: RenderNode | dict,
    prefix: str = "",
    *,
    reveal: int | None = None,
    show_harness: bool = True,
    name_text: str | None = None,
) -> tuple[int, int] | None:
    """Row-two cell span of the drawn name, clipped by the reveal."""
    return row2_layout(
        node, prefix, reveal=reveal, show_harness=show_harness, name_text=name_text
    ).name_span
