"""Chrome widgets: non-selectable static and the empty-tree call to action.

``NonSelectableStatic`` is the base for every chrome row that should
not participate in drag-to-select or ``Ctrl+A`` extraction.
``EmptyTreeState`` is the full-panel placeholder shown while the
participant tree is empty.
"""

from __future__ import annotations

from typing import ClassVar

from textual.selection import Selection
from textual.widgets import Static


class NonSelectableStatic(Static):
    """Static chrome excluded from drag and select-all extraction."""

    ALLOW_SELECT: ClassVar[bool] = False

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        return None


class EmptyTreeState(NonSelectableStatic):
    """Full-panel call to action shown while the participant tree is empty."""

    DEFAULT_CSS = """
    EmptyTreeState {
        width: 1fr;
        height: 1fr;
        content-align: center middle;
        text-align: center;
    }
    """
