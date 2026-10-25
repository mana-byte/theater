"""The row a new agent will occupy, asking for its directory where the directory will show."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from rich.cells import cell_len
from textual.content import Content

from regie.motions.wipe import Wipe, apply_wipe
from regie.render.glyphs import node_label
from regie.render.layout import Key
from regie.tree_nodes import RawRenderNode, RenderNode
from regie.widgets.name_editor import DirectoryEditor, InlineEditor
from regie.widgets.renameable import RenameableRow

_HINT = "⏎ spawn  esc cancel"


class SpawnLeaf(RenameableRow):
    """Three rows like an agent's; the directory is typed on the third, Tab completes."""

    can_focus = False
    _edit_row = 2
    _commit_unchanged = True

    DEFAULT_CSS = """
    SpawnLeaf { height: 3; padding: 0 2; background: $accent 12%; }
    """

    def __init__(self, node: RenderNode | dict, prefix: str, *, cont_prefix: str, key: Key) -> None:
        self._node: RawRenderNode = cast(RawRenderNode, node)
        self._prefix = prefix
        self._cont_prefix = cont_prefix
        self._value = str(node.get("cwd") or Path.cwd())
        self._reopen = False
        self._wipe: Wipe | None = None
        super().__init__()
        self.key = key
        self.update(self._render_label(), layout=False)

    def update_node(self, node: RenderNode | dict, prefix: str, *, cont_prefix: str) -> None:
        self._node, self._prefix, self._cont_prefix = cast(RawRenderNode, node), prefix, cont_prefix
        self.update(self._render_label(), layout=False)
        self._sync_rename_geometry()

    def _render_label(self) -> Content:
        width = self.content_size.width or None
        content = node_label(
            self._node,
            self._prefix,
            cont_prefix=self._cont_prefix,
            detail="" if self.renaming else self._value,
            cost=[(_HINT, "$text-muted")],
            width=width,
        )
        return content if self._wipe is None else apply_wipe(content, self._wipe, width=width)

    def set_wipe(self, wipe: Wipe | None) -> None:
        """Draw one frame of the sweep that greets this row when it opens, or end it."""
        if wipe != self._wipe:
            self._wipe = wipe
            self.update(self._render_label(), layout=False)

    def on_resize(self) -> None:
        self.update(self._render_label(), layout=False)
        self._sync_rename_geometry()

    def _name_span(self) -> tuple[int, int] | None:
        start = cell_len(self._cont_prefix)
        return start, start + cell_len(self._value)

    def _rename_value(self) -> str:
        return self._value

    def _make_editor(self, value: str) -> InlineEditor:
        return DirectoryEditor(
            value,
            base_dir=Path.cwd(),
            submit=self._rename_submitted,
            cancel=self._rename_cancelled,
        )

    def _commit_rename(self, name: str) -> None:
        submit = getattr(self.app, "submit_pending_spawn", None)
        error = submit(name) if callable(submit) else None
        if error:
            # A bad directory keeps the row: say why and let the user fix what they typed.
            self.app.notify(error, severity="warning")
            self._value, self._reopen = name, True

    def _edit_closed(self, *, committed: bool) -> None:
        if self._reopen:
            self._reopen = False
            self.run_worker(self.begin_rename(), exclusive=False)
            return
        if not committed:
            cancel = getattr(self.app, "cancel_pending_spawn", None)
            if callable(cancel):
                cancel()


__all__ = ["SpawnLeaf"]
