"""A row of the options menu: the root (a line folded, three open), an option, or a choice."""

from __future__ import annotations

from typing import cast

from textual import events
from textual.content import Content
from textual.message import Message

from regie.render.layout import Key, row_count
from regie.render.menu import menu_label
from regie.tree_nodes import RawRenderNode, RenderNode
from regie.widgets.chrome import NonSelectableStatic


class MenuRow(NonSelectableStatic):
    """Drawn like an agent leaf, but a click picks it and never stages anything."""

    can_focus = False

    class Clicked(Message):
        def __init__(self, key: Key) -> None:
            super().__init__()
            self.key = key

    DEFAULT_CSS = """
    MenuRow { height: 3; padding: 0 2; }
    MenuRow:hover { background: $accent 10%; }
    MenuRow.tree-cursor { background: $accent 20%; text-style: bold; }
    """

    def __init__(
        self,
        node: RenderNode | dict,
        prefix: str,
        *,
        cont_prefix: str,
        key: Key,
        is_first_root: bool,
    ) -> None:
        self._node: RawRenderNode = cast(RawRenderNode, node)
        self._prefix, self._cont_prefix = prefix, cont_prefix
        self._is_first_root = is_first_root
        super().__init__()
        self.key = key
        self._sync()

    def update_node(
        self, node: RenderNode | dict, prefix: str, *, cont_prefix: str, is_first_root: bool
    ) -> None:
        self._node, self._prefix, self._cont_prefix = cast(RawRenderNode, node), prefix, cont_prefix
        self._is_first_root = is_first_root
        self._sync()

    def set_cursor(self, on: bool) -> None:
        self.set_class(on, "tree-cursor")

    def _sync(self) -> None:
        self.styles.height = row_count(self.key, self._node)
        self.update(self._label(), layout=False)

    def _label(self) -> Content:
        return menu_label(
            self._node,
            self._prefix,
            self._cont_prefix,
            width=self.content_size.width or None,
            is_first_root=self._is_first_root,
        )

    def on_resize(self, _event: events.Resize) -> None:
        self.update(self._label(), layout=False)

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(self.Clicked(self.key))


__all__ = ["MenuRow"]
