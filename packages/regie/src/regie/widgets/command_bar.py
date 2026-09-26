"""The sidebar's on-demand input: one labelled field under the tree, never a floating window."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Static

from regie.widgets.directory_input import DirectoryInput

#: Called with the submitted text; a returned message keeps the bar open and shows it.
type Submit = Callable[[str], str | None]


class _BarInput(Input):
    """Swallow tree navigation while typing; Esc leaves the bar."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close_bar", show=False),
        Binding("up", "consume", show=False),
        Binding("down", "consume", show=False),
        Binding("ctrl+p", "consume", show=False),
    ]

    def action_close_bar(self) -> None:
        bar = next((node for node in self.ancestors if isinstance(node, CommandBar)), None)
        if bar is not None:
            bar.close()

    def action_consume(self) -> None:
        """A tree binding must not fire while the bar holds focus."""


class _BarDirectoryInput(DirectoryInput, _BarInput):
    """A directory field with Tab completion and the bar's key handling."""

    BINDINGS: ClassVar[list[BindingType]] = [*DirectoryInput.BINDINGS, *_BarInput.BINDINGS]


class CommandBar(Vertical):
    """Hidden until an action needs text; opens under the tree, closes on Enter or Esc."""

    DEFAULT_CSS = """
    CommandBar {
        display: none;
        height: auto;
        padding: 1 2 1 2;
        border-top: tall $secondary 60%;
        background: $boost;
    }
    CommandBar.-open { display: block; }
    CommandBar #command-bar-head { height: 1; margin-bottom: 1; }
    CommandBar #command-bar-title { width: 1fr; color: $secondary; text-style: bold; }
    CommandBar #command-bar-hint { width: auto; color: $text-muted; }
    CommandBar Input {
        background: transparent;
        border: none;
        padding: 0;
        height: 1;
    }
    CommandBar Input:focus { background: transparent; border: none; }
    CommandBar #command-bar-error { height: auto; margin-top: 1; color: $error; }
    CommandBar #command-bar-error.-empty { display: none; }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._submit: Submit | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="command-bar-head"):
            yield Static("", id="command-bar-title", markup=False)
            yield Static("", id="command-bar-hint", markup=False)
        yield Static("", id="command-bar-error", classes="-empty", markup=False)

    @property
    def is_open(self) -> bool:
        return self.has_class("-open")

    async def open(
        self,
        title: str,
        *,
        on_submit: Submit,
        placeholder: str = "",
        hint: str = "⏎ send   esc cancel",
        directory: Path | None = None,
    ) -> None:
        """Replace any open request with this one and focus its field."""
        await self._remove_field()
        self._submit = on_submit
        self.query_one("#command-bar-title", Static).update(title)
        self.query_one("#command-bar-hint", Static).update(hint)
        self._show_error("")
        field: Input = (
            _BarDirectoryInput(value=str(directory), base_dir=directory, id="command-bar-input")
            if directory is not None
            else _BarInput(placeholder=placeholder, id="command-bar-input")
        )
        await self.mount(field, before=self.query_one("#command-bar-error"))
        self.add_class("-open")
        field.focus()

    def close(self) -> None:
        """Hide the bar without submitting and hand the keyboard back to the tree."""
        if not self.is_open:
            return
        self.remove_class("-open")
        self._submit = None
        self.run_worker(self._remove_field(), exclusive=False)
        restore = getattr(self.app, "_restore_tree_focus", None)
        if callable(restore):
            restore()

    async def _remove_field(self) -> None:
        for field in self.query(Input):
            await field.remove()

    def _show_error(self, message: str) -> None:
        error = self.query_one("#command-bar-error", Static)
        error.update(message)
        error.set_class(not message, "-empty")

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self._show_error("")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        submit = self._submit
        if submit is None:
            return
        error = submit(event.value)
        if error:
            self._show_error(error)
            return
        self.close()

    def on_descendant_blur(self, _event: events.DescendantBlur) -> None:
        # Clicking anywhere else leaves the bar, as it leaves the inline rename.
        if self.is_open and not self.has_focus_within:
            self.call_after_refresh(self._close_if_unfocused)

    def _close_if_unfocused(self) -> None:
        if self.is_open and not self.has_focus_within:
            self.close()


__all__ = ["CommandBar"]
