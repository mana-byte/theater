"""Inline single-line editor for a participant's live-only name."""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from rich.cells import cell_len
from textual import events
from textual.binding import Binding, BindingType
from textual.widgets import Input


class NameEditor(Input):
    """One in-place name edit: Enter submits, Esc or blur cancels.

    Transparent and only as wide as its text, so the row around it stays readable.
    """

    DEFAULT_CSS = """
    NameEditor, NameEditor:focus, NameEditor.-textual-compact, NameEditor.-textual-compact:focus {
        background: transparent;
        border: none;
        padding: 0;
        height: 1;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", show=False),
        Binding("up", "consume", show=False),
        Binding("down", "consume", show=False),
        Binding("ctrl+p", "consume", show=False),
    ]

    def __init__(
        self,
        value: str,
        *,
        submit: Callable[[str], None],
        cancel: Callable[[], None],
    ) -> None:
        super().__init__(value=value, compact=True, id="name-editor")
        self._submit = submit
        self._cancel = cancel
        self._settled = False

    def _finish(self) -> None:
        if self._settled:
            return
        self._settled = True
        self.remove()

    def close(self) -> None:
        """Detach quietly: neither submit nor report cancellation."""
        self._settled = True
        self.remove()

    def on_input_changed(self, event: Input.Changed) -> None:
        self.styles.width = cell_len(event.value) + 1

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if self._settled:
            return
        value = event.value
        self._finish()
        self._submit(value)

    def action_cancel(self) -> None:
        if self._settled:
            return
        self._finish()
        self._cancel()

    def action_consume(self) -> None:
        """Swallow a tree binding while the editor holds focus."""

    def on_blur(self, _event: events.Blur) -> None:
        self.action_cancel()

    def on_click(self, event: events.Click) -> None:
        # The editor overlays the leaf: a click inside it must not stage the row.
        event.stop()


__all__ = ["NameEditor"]
