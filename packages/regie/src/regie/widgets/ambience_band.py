"""The strip under the tree's last row where the ambience scene draws."""

from __future__ import annotations

from textual.widgets import Static

from regie.ambience.render import render_band
from regie.ambience.scene import Cell


class AmbienceBand(Static):
    """Docked over the tree's own empty bottom rows, so it never covers a tree row."""

    can_focus = False

    DEFAULT_CSS = """
    AmbienceBand {
        display: none;
        dock: bottom;
        layer: overlay;
        width: 100%;
        height: 0;
        padding: 0 2;
    }
    """

    def show_cells(self, cells: list[Cell], width: int, height: int) -> None:
        self.styles.height = height + self.styles.padding.top + self.styles.padding.bottom
        self.display = True
        self.update(render_band(cells, width, height), layout=False)

    def clear(self) -> None:
        self.display = False


__all__ = ["AmbienceBand"]
