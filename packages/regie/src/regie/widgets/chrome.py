"""Non-selectable lightweight chrome used by the independent Textual app."""

from __future__ import annotations

from typing import ClassVar

from textual.selection import Selection
from textual.widgets import Static


class NonSelectableStatic(Static):
    """Prevent Textual text selection from leaking into terminal presentation."""

    ALLOW_SELECT: ClassVar[bool] = False

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        del selection
        return None


class StatusLine(NonSelectableStatic):
    """One bounded line for action and connection feedback."""


__all__ = ["NonSelectableStatic", "StatusLine"]
