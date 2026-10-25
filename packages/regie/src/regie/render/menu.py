"""Labels for the options menu's rows: the same rails and three rows as an agent leaf."""

from __future__ import annotations

from textual.content import Content

from regie.render.glyphs import _rail_above, fit_name, separator_prefix
from regie.tree_nodes import RawRenderNode

_RAIL = "$text dim"


def menu_label(
    node: RawRenderNode,
    prefix: str,
    cont_prefix: str,
    *,
    width: int | None = None,
    is_first_root: bool = False,
) -> Content:
    """One menu row: ``+ options`` (a line folded, three open), an option or choice (three), info.

    An option or choice is a rail, then its glyph and title, then its help in dim text under the
    title. *width* cuts what would not fit, so a long title ends in ``…`` instead of wrapping.
    """
    kind = node.get("kind")
    if kind == "menu_root":
        return _root_label(node, prefix, is_first_root=is_first_root)
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
    title = _fit(node.get("title", ""), prefix, width, 2)
    help_text = _fit(node.get("help") or "", cont_prefix, width, 2)
    return Content.assemble(
        (_rail_above(prefix), _RAIL),
        "\n",
        (prefix, _RAIL),
        (f"{glyph} ", glyph_style + dim),
        (title, f"$text{dim}"),
        "\n",
        (cont_prefix, _RAIL),
        ("  ", ""),
        (help_text, "$text-muted dim"),
    )


def _root_label(node: RawRenderNode, prefix: str, *, is_first_root: bool) -> Content:
    title = node.get("title", "")
    if not node.get("open"):
        return Content.assemble((prefix, _RAIL), (title, "$text-muted"))
    return Content.assemble(
        *([] if is_first_root else [(_rail_above(prefix), _RAIL)]),
        "\n",
        (prefix, _RAIL),
        (title, "bold $secondary"),
        "\n",
        (separator_prefix(prefix), _RAIL),
        (node.get("help") or "", "$text-muted dim"),
    )


def _fit(text: str, prefix: str, width: int | None, lead: int) -> str:
    """*text* cut to what is left of *width* after the rails and *lead* cells."""
    return text if width is None else fit_name(text, max(1, width - len(prefix) - lead))


__all__ = ["menu_label"]
