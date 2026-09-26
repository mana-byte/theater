"""Selectable, renameable, collapsible section headings in the participant tree."""

from __future__ import annotations

from rich.cells import cell_len
from textual import events
from textual.content import Content

from regie.animations.routes import LeafOverlay
from regie.render.glyphs import separator_label, separator_name_span
from regie.render.layout import Key
from regie.widgets.renameable import RenameableRow


class SeparatorRow(RenameableRow):
    can_focus = False

    DEFAULT_CSS = """
    SeparatorRow { height: 3; padding: 0 2; }
    SeparatorRow:hover { background: $accent 10%; }
    SeparatorRow.tree-cursor { background: $accent 20%; text-style: bold; }
    """

    def __init__(self, node: dict, prefix: str, *, key: Key, is_first_root: bool = False) -> None:
        self._node = node
        self._prefix = prefix
        self._is_first_root = is_first_root
        self._overlay: LeafOverlay | None = None
        super().__init__()
        self.key = key
        self.update(self._render_label(), layout=False)

    @property
    def _label_name(self) -> str:
        return str(self._node.get("name", ""))

    def update_node(self, node: dict, prefix: str, *, is_first_root: bool = False) -> None:
        self._node = node
        self._prefix = prefix
        self._is_first_root = is_first_root
        self.update(self._render_label(), layout=False)
        self._sync_rename_geometry()

    def set_overlay(self, overlay: LeafOverlay | None) -> None:
        self._overlay = overlay
        self.update(self._render_label(), layout=False)

    def _render_label(self) -> Content:
        name = self._label_name
        return separator_label(
            " " * cell_len(name.upper()) if self.renaming else name,
            self._prefix,
            count=int(self._node.get("count") or 0),
            collapsed=bool(self._node.get("collapsed")),
            is_first_root=self._is_first_root,
            overlay=self._overlay,
        )

    def set_cursor(self, selected: bool) -> None:
        self.set_class(selected, "tree-cursor")

    def _name_span(self) -> tuple[int, int] | None:
        return separator_name_span(self._label_name, self._prefix)

    def _rename_value(self) -> str:
        return self._label_name

    def _commit_rename(self, name: str) -> None:
        rename = getattr(self.app, "rename_separator", None)
        if callable(rename):
            rename(self.key[1], name)

    async def on_click(self, event: events.Click) -> None:
        event.stop()
        select_tree_item = getattr(self.app, "select_tree_item", None)
        if callable(select_tree_item):
            select_tree_item(self.key, self.key[1])
        if event.button != 1 or event.chain != 1:
            return
        if self._name_clicked(event):
            await self.begin_rename()
        elif self._chevron_clicked(event):
            toggle = getattr(self.app, "toggle_separator", None)
            if callable(toggle):
                toggle(self.key[1])

    def _chevron_clicked(self, event: events.Click) -> bool:
        offset = event.get_content_offset(self)
        start = cell_len(self._prefix)
        return offset is not None and offset.y == 1 and start <= offset.x < start + 2


__all__ = ["SeparatorRow"]
