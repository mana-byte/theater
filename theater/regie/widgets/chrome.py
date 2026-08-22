"""Chrome widgets: non-selectable static and the empty-tree call to action.

``NonSelectableStatic`` is the base for every chrome row that should
not participate in drag-to-select or ``Ctrl+A`` extraction.
``EmptyTreeState`` is the full-panel placeholder shown while the
participant tree is empty.
"""

from __future__ import annotations

from typing import ClassVar

from textual.content import Content
from textual.selection import Selection
from textual.widgets import Static

from theater.constants.regie import (
    REGIE_EMPTY_TREE_SHORTCUT,
    REGIE_EMPTY_TREE_SHORTCUT_STYLE,
    REGIE_EMPTY_TREE_TAIL,
)
from theater.regie.animations.reveal import StyledPart, clip_parts


class NonSelectableStatic(Static):
    """Static chrome excluded from drag and select-all extraction."""

    ALLOW_SELECT: ClassVar[bool] = False

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        return None


class EmptyTreeState(NonSelectableStatic):
    """Full-panel call to action shown while the participant tree is empty."""

    _PARTS: tuple[StyledPart, ...] = (
        (REGIE_EMPTY_TREE_SHORTCUT, REGIE_EMPTY_TREE_SHORTCUT_STYLE),
        (REGIE_EMPTY_TREE_TAIL, "$text-muted"),
    )

    DEFAULT_CSS = """
    EmptyTreeState {
        width: 1fr;
        height: 1fr;
        content-align: center middle;
        text-align: center;
    }
    """

    def __init__(self, *, reveal: int | None = None, **kwargs) -> None:
        self._reveal = reveal
        super().__init__(self._hint_content(), **kwargs)

    @property
    def required_reveal_width(self) -> int:
        return sum(len(part if isinstance(part, str) else part[0]) for part in self._PARTS)

    def set_reveal(self, reveal: int | None) -> None:
        """Set visible startup characters, or None for the full hint."""
        if reveal == self._reveal:
            return
        self._reveal = reveal
        self.update(self._hint_content(), layout=False)

    def _hint_content(self) -> Content:
        parts = self._PARTS if self._reveal is None else clip_parts(self._PARTS, self._reveal)
        return Content.assemble(*parts)
