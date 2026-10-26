"""Labels for the options menu's rows: the same rails and three rows as an agent leaf."""

from __future__ import annotations

from textual.content import Content

from regie.motions.marquee import marquee_cells
from regie.render.glyphs import fit_name, rail_above, separator_prefix
from regie.tree_nodes import RawRenderNode

_RAIL = "$text dim"


def menu_label(
    node: RawRenderNode,
    prefix: str,
    cont_prefix: str,
    *,
    width: int | None = None,
    is_first_root: bool = False,
    editing: bool = False,
    help_offset: int | None = None,
) -> Content:
    """One menu row: ``+ options`` (a line folded, three open), an option or choice (three), info.

    An option or choice is a rail, then its glyph and title, then its help in dim text under the
    title. *width* cuts what would not fit, so a long title ends in ``…`` instead of wrapping; a
    long help scrolls instead while *help_offset* says how far (the row is in focus).
    """
    kind = node.get("kind")
    if kind == "menu_root":
        return _root_label(node, prefix, is_first_root=is_first_root, editing=editing)
    if kind == "info":
        text = _fit(node.get("title", ""), prefix, width, 0)
        return Content.assemble((prefix, _RAIL), (text, "$text-muted italic"))
    dim = "" if node.get("enabled", True) else " dim"
    chevron = node.get("chevron")
    glyph, glyph_style = (
        (chevron, "$secondary")
        if chevron
        else (node.get("glyph") or " ", node.get("style") or "$text")
    )
    shortcut = node.get("shortcut")
    key = [(" · ", _RAIL), (shortcut, "$text dim")] if shortcut else []
    title = _fit(node.get("title", ""), prefix, width, 2 + sum(len(text) for text, _ in key))
    help_text = node.get("help") or ""
    if width is not None:
        room = help_room(cont_prefix, width)
        help_text = (
            fit_name(help_text, room)
            if help_offset is None
            else marquee_cells(help_text, room, help_offset)
        )
    return Content.assemble(
        (rail_above(prefix), _RAIL),
        "\n",
        (prefix, _RAIL),
        (f"{glyph} ", glyph_style + dim),
        (title, f"$text{dim}"),
        *key,
        "\n",
        (cont_prefix, _RAIL),
        ("  ", ""),
        (help_text, "$text-muted dim"),
    )


def help_room(cont_prefix: str, width: int) -> int:
    """Cells an option's help has on its third row, after the rails and the glyph's column."""
    return max(1, width - len(cont_prefix) - 2)


def _root_label(node: RawRenderNode, prefix: str, *, is_first_root: bool, editing: bool) -> Content:
    title, shortcut = node.get("title", ""), node.get("shortcut")
    key = [(" · ", _RAIL), (shortcut, "$text dim")] if shortcut else []
    if not node.get("open"):
        return Content.assemble((prefix, _RAIL), (title, "$text-muted"), *key)
    return Content.assemble(
        *([] if is_first_root else [(rail_above(prefix), _RAIL)]),
        "\n",
        (prefix, _RAIL),
        (title, "bold $secondary"),
        *key,
        "\n",
        (separator_prefix(prefix), _RAIL),
        *_third_row(node, editing),
    )


def _third_row(node: RawRenderNode, editing: bool) -> list:
    """The hint, or the filter: ``/ `` then what was typed (left to the editor while it is open)."""
    query = node.get("filter") or ""
    if editing:
        return [("/ ", "$text-muted")]
    if query:
        return [("/ ", "$text-muted"), (query, "$text")]
    return [(node.get("help") or "", "$text-muted dim")]


def _fit(text: str, prefix: str, width: int | None, lead: int) -> str:
    """*text* cut to what is left of *width* after the rails and *lead* cells."""
    return text if width is None else fit_name(text, max(1, width - len(prefix) - lead))


__all__ = ["help_room", "menu_label"]
