"""Shared sweep rendering for participant and pending spawn rows."""

from __future__ import annotations

from textual.content import Content

from regie.motions.wipe import Wipe, apply_wipe
from regie.widgets.renameable import RenameableRow


class WipeMixin(RenameableRow):
    _wipe: Wipe | None

    def _apply_wipe(self, content: Content, *, width: int | None) -> Content:
        return (
            content
            if self._wipe is None
            else apply_wipe(content, self._wipe, width=width, blend=not self.app.native_ansi_color)
        )

    def set_wipe(self, wipe: Wipe | None) -> None:
        """Draw one frame of the sweep on this row, or end it."""
        if wipe != self._wipe:
            self._wipe = wipe
            self.update(self._render_label(), layout=False)
