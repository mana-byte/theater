"""Modal prompts for participant controls.

Steer, queued followups, and settings changes need text the tree has no way to
type, so each action opens one small centered dialog, dismisses with a value
or `None`, and hands the answer back through the callback `push_screen`
already supports. Esc always cancels; Enter always submits what is there.
Nothing else is captured here — no client, no policy — so every decision
about what the text does remains the daemon's.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label


class ControlPromptScreen(ModalScreen[str | None]):
    """One-line prompt for a steer message or a followup prompt."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel", priority=True)]

    DEFAULT_CSS = """
    ControlPromptScreen {
        align: center middle;
    }
    #control-prompt-dialog {
        width: 60;
        height: auto;
        padding: 1 2;
        border: solid $accent 40%;
        background: $surface;
    }
    #control-prompt-dialog Label {
        width: 1fr;
        text-style: bold;
        margin-bottom: 1;
    }
    #control-prompt-dialog Input {
        width: 1fr;
    }
    """

    def __init__(self, title: str, placeholder: str) -> None:
        super().__init__()
        self._title = title
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="control-prompt-dialog"):
            yield Label(self._title)
            yield Input(placeholder=self._placeholder)

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        self.dismiss(text or None)


class SettingsPromptScreen(ModalScreen[tuple[str, str] | None]):
    """Model and reasoning-effort prompt for an idle settings update.

    Enter on either field submits both; the app decides whether the pair is
    enough to ask the daemon. Both fields are free text here because the
    daemon owns the model and reasoning allowlists — the régie must not keep
    its own copy of what is legal.
    """

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel", priority=True)]

    DEFAULT_CSS = """
    SettingsPromptScreen {
        align: center middle;
    }
    #settings-prompt-dialog {
        width: 60;
        height: auto;
        padding: 1 2;
        border: solid $accent 40%;
        background: $surface;
    }
    #settings-prompt-dialog Label {
        width: 1fr;
        text-style: bold;
        margin-bottom: 1;
    }
    #settings-prompt-dialog Input {
        width: 1fr;
        margin-bottom: 1;
    }
    """

    def __init__(self, *, model: str = "", reasoning_effort: str = "") -> None:
        super().__init__()
        self._model = model
        self._reasoning_effort = reasoning_effort

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-prompt-dialog"):
            yield Label("Update session settings")
            yield Input(
                value=self._model,
                placeholder="model (optional)",
                id="settings-model",
            )
            yield Input(
                value=self._reasoning_effort,
                placeholder="reasoning effort (optional)",
                id="settings-reasoning",
            )

    def on_mount(self) -> None:
        self.query_one("#settings-model", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        del event  # Either field's Enter submits the pair as a whole.
        model = self.query_one("#settings-model", Input).value.strip()
        reasoning = self.query_one("#settings-reasoning", Input).value.strip()
        self.dismiss((model, reasoning))
