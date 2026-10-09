"""Selectable, renameable, collapsible section headings in the participant tree."""

from __future__ import annotations

from rich.cells import cell_len
from textual import events
from textual.content import Content
from textual.message import Message

from regie.motions.routes import LeafOverlay
from regie.motions.spinner import advance_spinner_frame
from regie.render.glyphs import separator_label, separator_name_span, with_stage_marker
from regie.render.layout import Key
from regie.ui_constants import REGIE_LEAF_SPINNER_INTERVAL
from regie.widgets.animation_clock import AnimationSubscription, animation_clock
from regie.widgets.leaf import StageMarker
from regie.widgets.renameable import RenameableRow


class SeparatorRow(RenameableRow):
    can_focus = False

    class Hovered(Message):
        """The pointer entered or left a heading, so its section's branches can light up."""

        def __init__(self, key: Key, hovered: bool) -> None:
            super().__init__()
            self.key, self.hovered = key, hovered

    DEFAULT_CSS = """
    SeparatorRow { height: 3; padding: 0 2; }
    SeparatorRow:hover { background: $accent 10%; }
    SeparatorRow.tree-cursor { background: $accent 20%; text-style: bold; }
    SeparatorRow.tree-staged { padding: 0 2 0 0; }
    """

    def __init__(self, node: dict, prefix: str, *, key: Key, is_first_root: bool = False) -> None:
        self._node = node
        self._prefix = prefix
        self._is_first_root = is_first_root
        self._overlay: LeafOverlay | None = None
        self._highlight: LeafOverlay | None = None
        self._stage_marker: StageMarker | None = None
        self._frame = 0
        self._spinner_sub: AnimationSubscription | None = None
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
        self._sync_spinner()
        self.update(self._render_label(), layout=False)
        self._sync_rename_geometry()

    @property
    def fold_status(self) -> str | None:
        status = self._node.get("folded_status")
        return status if isinstance(status, str) else None

    def on_mount(self) -> None:
        self._sync_spinner()

    def _sync_spinner(self) -> None:
        """Spin only while a hidden agent is working, as that agent's own row would."""
        if self.fold_status == "working":
            if self._spinner_sub is None and self.is_attached:
                self._spinner_sub = animation_clock(self.app).subscribe(
                    REGIE_LEAF_SPINNER_INTERVAL, self._tick
                )
        elif self._spinner_sub is not None:
            self._spinner_sub.stop()
            self._spinner_sub = None

    def on_unmount(self) -> None:
        if self._spinner_sub is not None:
            self._spinner_sub.stop()
            self._spinner_sub = None

    def _tick(self) -> None:
        self._frame = advance_spinner_frame(self._frame)
        self.update(self._render_label(), layout=False)

    def set_highlight(self, highlight: LeafOverlay | None) -> None:
        """Its own branch and the rail down to its section, heavy while hovered or selected."""
        if highlight != self._highlight:
            self._highlight = highlight
            self.update(self._render_label(), layout=False)

    def set_overlay(self, overlay: LeafOverlay | None) -> None:
        self._overlay = overlay
        self.update(self._render_label(), layout=False)

    def _render_label(self) -> Content:
        name = self._label_name
        return with_stage_marker(self._heading(name), self._stage_marker)

    def _heading(self, name: str) -> Content:
        return separator_label(
            " " * cell_len(name.upper()) if self.renaming else name,
            self._prefix,
            count=None if self.renaming else int(self._node.get("count") or 0),
            collapsed=bool(self._node.get("collapsed")),
            status=self.fold_status,
            frame=self._frame,
            is_first_root=self._is_first_root,
            overlay=self._overlay or self._highlight,  # a live route takes over
        )

    @property
    def folded_ids(self) -> frozenset[str]:
        """Agents this folded heading hides; empty while it is open."""
        folded = self._node.get("folded")
        return frozenset(folded) if isinstance(folded, list) else frozenset()

    def set_stage_marker(self, marker: StageMarker | None) -> None:
        """Carry the stage bar of an agent hidden in this fold."""
        if marker == self._stage_marker:
            return
        self._stage_marker = marker
        self.set_class(marker is not None, "tree-staged")
        self.update(self._render_label(), layout=False)
        self._sync_rename_geometry()

    def set_cursor(self, selected: bool) -> None:
        self.set_class(selected, "tree-cursor")

    def _name_span(self) -> tuple[int, int] | None:
        start, end = separator_name_span(self._label_name, self._prefix)
        gutter = 2 if self._stage_marker is not None else 0
        return start + gutter, end + gutter

    def _rename_value(self) -> str:
        return self._label_name

    def _commit_rename(self, name: str) -> None:
        rename = getattr(self.app, "rename_separator", None)
        if callable(rename):
            rename(self.key[1], name)

    def _edit_closed(self, *, committed: bool) -> None:
        closed = getattr(self.app, "separator_edit_closed", None)
        if callable(closed):
            closed(self.key[1], committed=committed)

    def on_enter(self, _event: events.Enter) -> None:
        self.post_message(self.Hovered(self.key, True))

    def on_leave(self, _event: events.Leave) -> None:
        self.post_message(self.Hovered(self.key, False))

    async def on_click(self, event: events.Click) -> None:
        event.stop()
        select_tree_item = getattr(self.app, "select_tree_item", None)
        if callable(select_tree_item):
            select_tree_item(self.key, self.key[1])
        if event.button != 1 or event.chain != 1:
            return
        if self._name_clicked(event):
            await self.begin_rename()
            return
        # Anywhere else on the heading folds or unfolds its section.
        toggle = getattr(self.app, "toggle_separator", None)
        if callable(toggle):
            toggle(self.key[1])


__all__ = ["SeparatorRow"]
