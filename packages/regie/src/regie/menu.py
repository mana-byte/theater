"""The options menu closing the tree: what it holds and what shows for the cursor and the view.

Pure data: no Textual. The app builds the items (callbacks are its own methods); this module
decides which rows exist, so the tree draws them with the same rails as the agents above.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from regie.tree_nodes import RawRenderNode, RenderNode
from regie.ui_constants import (
    REGIE_MENU_HINT,
    REGIE_MENU_MORE_ID,
    REGIE_MENU_PREVIEW_ROWS,
    REGIE_MENU_TITLE,
)

#: The root's row id; every other row's id is its path from it, e.g. ``spawn/codex``.
ROOT_ID = "options"

type LoadState = Literal["ready", "loading", "failed"]


@dataclass(frozen=True, slots=True)
class MenuItem:
    """One option or one choice of a sub-menu."""

    id: str
    title: str
    help: str | None = None
    glyph: str = "·"
    style: str = "$text"
    enabled: bool = True
    #: What picking it does; None for an option that only unfolds into *choices*.
    run: Callable[[], object] | None = None
    #: A sub-menu's choices, with the state of the list that holds them.
    choices: Loaded | None = None
    #: A pick that leaves the menu open (a theme, to be compared with the next).
    keeps_menu: bool = False


@dataclass(frozen=True, slots=True)
class Loaded:
    """The choices of a sub-menu: ready, still loading, or failed, with a note for the empty row."""

    items: tuple[MenuItem, ...] = ()
    state: LoadState = "ready"
    note: str | None = None


@dataclass(frozen=True, slots=True)
class MenuView:
    """What the human has opened: options unfolded, lists shown whole, and the filter typed."""

    expanded: frozenset[str] = field(default_factory=frozenset)
    more: frozenset[str] = field(default_factory=frozenset)
    query: str = ""


def _matches(item: MenuItem, needle: str) -> bool:
    """Whether the lower-case *needle* is in the item's title or help."""
    return needle in item.title.lower() or needle in (item.help or "").lower()


def _info(path: str, text: str) -> RawRenderNode:
    return {"kind": "info", "id": path, "title": text}


def _row(item: MenuItem, path: str, chevron: str | None) -> RawRenderNode:
    row: RawRenderNode = {
        "kind": "menu",
        "id": path,
        "title": item.title,
        "help": item.help,
        "glyph": item.glyph,
        "style": item.style,
        "enabled": item.enabled,
        "children": [],
    }
    if chevron:
        row["chevron"] = chevron
    return row


def _choice_rows(option: MenuItem, view: MenuView) -> list[RenderNode]:
    """The rows under an unfolded option: its choices, a few of a long list, or why none."""
    assert option.choices is not None
    choices = option.choices
    path = option.id
    if choices.state == "loading":
        return [_info(f"{path}/loading", "loading…")]
    if choices.state == "failed":
        return [_info(f"{path}/failed", choices.note or "could not load")]
    items = choices.items
    needle = view.query.strip().lower()
    if needle:
        items = tuple(item for item in items if _matches(item, needle))
    if not items:
        return [_info(f"{path}/empty", choices.note or "nothing here")]
    shown = items
    if not needle and path not in view.more and len(items) > REGIE_MENU_PREVIEW_ROWS + 1:
        shown = items[:REGIE_MENU_PREVIEW_ROWS]
    rows: list[RenderNode] = [_row(item, f"{path}/{item.id}", None) for item in shown]
    if len(shown) < len(items):
        rest = len(items) - len(shown)
        more = MenuItem(REGIE_MENU_MORE_ID, f"… {rest} more", "enter shows them all")
        rows.append(_row(more, f"{path}/{REGIE_MENU_MORE_ID}", None))
    return rows


def _option_rows(items: Sequence[MenuItem], view: MenuView) -> list[RenderNode]:
    rows: list[RenderNode] = []
    needle = view.query.strip().lower()
    for item in items:
        if item.choices is None:
            if not needle or _matches(item, needle):
                rows.append(_row(item, item.id, None))
            continue
        loaded = item.choices.items
        hit = bool(needle) and any(_matches(choice, needle) for choice in loaded)
        if needle and not hit and not _matches(item, needle):
            continue
        unfolded = item.id in view.expanded or hit
        node = _row(item, item.id, "▾" if unfolded else "▸")
        if unfolded:
            node["children"] = _choice_rows(item, view)
        rows.append(node)
    return rows


def menu_nodes(items: Sequence[MenuItem], view: MenuView, *, open: bool) -> RawRenderNode:
    """The menu as a tree root: closed to one line, open to its options and the choices unfolded."""
    return {
        "kind": "menu_root",
        "id": ROOT_ID,
        "title": REGIE_MENU_TITLE,
        "help": REGIE_MENU_HINT,
        "open": open,
        "filter": view.query,
        "children": _option_rows(items, view) if open else [],
    }


def find_item(items: Sequence[MenuItem], path: str) -> MenuItem | None:
    """The option or choice at *path*, from the ids of its rows (``spawn``, ``spawn/codex``)."""
    option_id, _, choice_id = path.partition("/")
    option = next((item for item in items if item.id == option_id), None)
    if option is None or not choice_id or option.choices is None:
        return option if not choice_id else None
    return next((item for item in option.choices.items if item.id == choice_id), None)


__all__ = [
    "ROOT_ID",
    "Loaded",
    "MenuItem",
    "MenuView",
    "find_item",
    "menu_nodes",
]
