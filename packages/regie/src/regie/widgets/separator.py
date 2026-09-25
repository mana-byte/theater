"""Selectable one-line dividers in the participant tree."""

from __future__ import annotations

from textual import events
from textual.content import Content
from textual.widgets import Label

from regie.animations.routes import LeafOverlay
from regie.render.glyphs import separator_label
from regie.render.layout import Key


class SeparatorRow(Label):
    can_focus = False

    DEFAULT_CSS = """
    SeparatorRow { height: 1; padding: 0 2; }
    SeparatorRow:hover { background: $accent 10%; }
    SeparatorRow.tree-cursor { background: $accent 20%; text-style: bold; }
    """

    def __init__(self, node: dict, prefix: str, *, key: Key) -> None:
        self._node = node
        self._prefix = prefix
        self._overlay: LeafOverlay | None = None
        super().__init__(self._render_label())
        self.key = key

    def update_node(self, node: dict, prefix: str) -> None:
        changed = node.get("name") != self._node.get("name") or prefix != self._prefix
        self._node = node
        self._prefix = prefix
        self.update(self._render_label(), layout=changed)

    def set_overlay(self, overlay: LeafOverlay | None) -> None:
        self._overlay = overlay
        self.update(self._render_label(), layout=False)

    def _render_label(self) -> Content:
        return separator_label(str(self._node.get("name", "")), self._prefix, overlay=self._overlay)

    def set_cursor(self, selected: bool) -> None:
        self.set_class(selected, "tree-cursor")

    def on_click(self, event: events.Click) -> None:
        event.stop()
        select_tree_item = getattr(self.app, "select_tree_item", None)
        if callable(select_tree_item):
            select_tree_item(self.key, self.key[1])


__all__ = ["SeparatorRow"]
