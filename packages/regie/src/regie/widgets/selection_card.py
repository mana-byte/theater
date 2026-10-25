"""The card under the tree's last row, drawn only where the tree leaves room."""

from __future__ import annotations

from textual.content import Content

from regie.widgets.chrome import NonSelectableStatic


class SelectionCard(NonSelectableStatic):
    """A few dim lines about the row in focus; never takes input."""

    can_focus = False

    DEFAULT_CSS = """
    SelectionCard { height: auto; margin: 1 0 0 0; padding: 0 2; display: none; }
    """

    #: Rows the card takes with its spacer above, so the tree can tell whether it fits.
    SPACER_ROWS = 1

    def show_lines(self, lines: list[Content]) -> None:
        self.update(Content("\n").join(lines), layout=False)
        self.styles.height = len(lines)
        self.display = True

    def hide(self) -> None:
        self.display = False


__all__ = ["SelectionCard"]
