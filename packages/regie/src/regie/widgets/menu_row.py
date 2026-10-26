"""A row of the options menu: the root (a line folded, three open), an option, or a choice."""

from __future__ import annotations

from typing import cast

from rich.cells import cell_len
from textual import events
from textual.content import Content
from textual.message import Message

from regie.motions.marquee import overflows_cells
from regie.render.glyphs import separator_prefix
from regie.render.layout import Key, row_count
from regie.render.menu import help_room, menu_label
from regie.tree_nodes import RawRenderNode, RenderNode
from regie.widgets.animation_clock import AnimationSubscription
from regie.widgets.marquee import MarqueeMixin
from regie.widgets.name_editor import FilterEditor, InlineEditor


class MenuRow(MarqueeMixin):
    """Drawn like an agent leaf, but a click picks it and never stages anything.

    The root also hosts the filter: its editor sits on the third row, after ``/ ``.
    """

    _edit_row = 2
    _commit_unchanged = True  # Enter on the untouched filter keeps it

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
        self._cursor = self._hovered = False  # either one scrolls a long help
        self._marquee_sub: AnimationSubscription | None = None
        self._marquee_offset = 0
        super().__init__()
        self.key = key
        self._sync()

    def update_node(
        self, node: RenderNode | dict, prefix: str, *, cont_prefix: str, is_first_root: bool
    ) -> None:
        if node.get("help") != self._node.get("help"):
            self._stop_marquee()
        self._node, self._prefix, self._cont_prefix = cast(RawRenderNode, node), prefix, cont_prefix
        self._is_first_root = is_first_root
        self._sync()
        self._sync_marquee()

    def set_cursor(self, on: bool) -> None:
        if on != self._cursor:
            self._cursor = on
            self.set_class(on, "tree-cursor")
            self._restart_marquee()

    def _sync(self) -> None:
        self.styles.height = row_count(self.key, self._node)
        self.update(self._label(), layout=False)
        self._sync_rename_geometry()

    def _label(self) -> Content:
        return menu_label(
            self._node,
            self._prefix,
            self._cont_prefix,
            width=self.content_size.width or None,
            is_first_root=self._is_first_root,
            editing=self.renaming,
            help_offset=self._marquee_offset if self._help_scrolls() else None,
        )

    # ---- a long help scrolls while the row is in focus, as a participant's description does ---

    def _help_scrolls(self) -> bool:
        width = self.content_size.width
        help_text = self._node.get("help") or ""
        return (
            self._node.get("kind") == "menu"
            and (self._cursor or self._hovered)
            and bool(width)
            and overflows_cells(help_text, help_room(self._cont_prefix, width))
        )

    def _should_marquee(self) -> bool:
        return self._help_scrolls()

    def on_mount(self) -> None:
        self._sync_marquee()

    def on_unmount(self) -> None:
        self._stop_marquee()

    def on_enter(self, _event: events.Enter) -> None:
        self._hovered = True
        self._restart_marquee()

    def on_leave(self, _event: events.Leave) -> None:
        self._hovered = False
        self._restart_marquee()

    # ---- the filter, on the root's third row -----------------------------------------------

    def _render_label(self) -> Content:
        return self._label()

    def _name_span(self) -> tuple[int, int] | None:
        if self._node.get("kind") != "menu_root" or not self._node.get("open"):
            return None
        start = cell_len(separator_prefix(self._prefix)) + len("/ ")
        return start, start + cell_len(self._rename_value())

    def _rename_value(self) -> str:
        return str(self._node.get("filter") or "")

    def _make_editor(self, value: str) -> InlineEditor:
        return FilterEditor(
            value,
            submit=self._rename_submitted,
            cancel=self._rename_cancelled,
            changed=self._filter_changed,
        )

    def _filter_changed(self, value: str) -> None:
        if callable(changed := getattr(self.app, "menu_filter_changed", None)):
            changed(value)

    def _commit_rename(self, name: str) -> None:
        if callable(closed := getattr(self.app, "menu_filter_closed", None)):
            closed(kept=True)

    def _edit_closed(self, *, committed: bool) -> None:
        if not committed and callable(closed := getattr(self.app, "menu_filter_closed", None)):
            closed(kept=False)

    def on_resize(self, _event: events.Resize) -> None:
        self._restart_marquee()

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(self.Clicked(self.key))


__all__ = ["MenuRow"]
