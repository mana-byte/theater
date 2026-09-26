"""Inline single-line editors that sit on a tree row: a name, or a spawn directory."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from rich.cells import cell_len
from textual import events
from textual.binding import Binding, BindingType
from textual.widgets import Input

from regie.widgets.directory_input import DirectoryInput


class InlineEditor(Input):
    """One in-place edit: Enter submits, Esc or blur cancels.

    Transparent and only as wide as its text, so the row around it stays readable.
    """

    DEFAULT_CSS = """
    InlineEditor, InlineEditor:focus, InlineEditor.-textual-compact,
    InlineEditor.-textual-compact:focus {
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
        *,
        submit: Callable[[str], None],
        cancel: Callable[[], None],
        **input_options: object,
    ) -> None:
        super().__init__(compact=True, **input_options)  # type: ignore[arg-type]
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
        # The editor overlays the row: a click inside it must not act on the row.
        event.stop()


class NameEditor(InlineEditor):
    """Edit an agent's live alias or a separator's name."""

    def __init__(
        self, value: str, *, submit: Callable[[str], None], cancel: Callable[[], None]
    ) -> None:
        super().__init__(value=value, submit=submit, cancel=cancel)


class DirectoryEditor(InlineEditor, DirectoryInput):
    """Edit a spawn directory; Tab completes it."""

    BINDINGS: ClassVar[list[BindingType]] = [*InlineEditor.BINDINGS, *DirectoryInput.BINDINGS]

    def __init__(
        self,
        value: str,
        *,
        base_dir: Path,
        submit: Callable[[str], None],
        cancel: Callable[[], None],
    ) -> None:
        super().__init__(value=value, base_dir=base_dir, submit=submit, cancel=cancel)


__all__ = ["DirectoryEditor", "InlineEditor", "NameEditor"]
