"""Non-selectable lightweight chrome used by the independent Textual app."""

from __future__ import annotations

from typing import ClassVar

from textual.content import Content
from textual.selection import Selection
from textual.widgets import Static

from regie.animations.reveal import StyledPart, clip_parts
from regie.ui_constants import (
    REGIE_EMPTY_TREE_SHORTCUT,
    REGIE_EMPTY_TREE_SHORTCUT_STYLE,
    REGIE_EMPTY_TREE_TAIL,
)


class NonSelectableStatic(Static):
    """Prevent Textual text selection from leaking into terminal presentation."""

    ALLOW_SELECT: ClassVar[bool] = False

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        del selection
        return None


class StatusLine(NonSelectableStatic):
    """One bounded line for action and connection feedback."""


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
        if reveal == self._reveal:
            return
        self._reveal = reveal
        self.update(self._hint_content(), layout=False)

    def _hint_content(self) -> Content:
        parts = self._PARTS if self._reveal is None else clip_parts(self._PARTS, self._reveal)
        return Content.assemble(*parts)


__all__ = ["EmptyTreeState", "NonSelectableStatic", "StatusLine"]
