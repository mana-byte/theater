"""Small public-action dialogs for Régie controls and catalog-backed spawn."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label

from regie.widgets.directory_input import DirectoryInput, normalize_directory


class ControlPromptScreen(ModalScreen[str | None]):
    """Collect one bounded prompt; policy remains with the public daemon API."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", priority=True)]

    DEFAULT_CSS = """
    ControlPromptScreen { align: center middle; }
    #control-prompt {
        width: 60;
        height: auto;
        padding: 1 2;
        border: solid $accent 40%;
        background: $surface;
    }
    #control-prompt Label { width: 1fr; text-style: bold; margin-bottom: 1; }
    #control-prompt Input { width: 1fr; }
    """

    def __init__(self, title: str, placeholder: str) -> None:
        super().__init__()
        self._title = title
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="control-prompt"):
            yield Label(self._title)
            yield Input(placeholder=self._placeholder, id="control-prompt-input")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)


class SettingsPromptScreen(ModalScreen[tuple[str, str] | None]):
    """Collect optional model and reasoning fields without owning their vocabulary."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", priority=True)]

    DEFAULT_CSS = """
    SettingsPromptScreen { align: center middle; }
    #settings-prompt {
        width: 60;
        height: auto;
        padding: 1 2;
        border: solid $accent 40%;
        background: $surface;
    }
    #settings-prompt Label { width: 1fr; text-style: bold; margin-bottom: 1; }
    #settings-prompt Input { width: 1fr; margin-bottom: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-prompt"):
            yield Label("Update session settings")
            yield Input(placeholder="model (optional)", id="settings-model")
            yield Input(placeholder="reasoning effort (optional)", id="settings-reasoning")

    def on_mount(self) -> None:
        self.query_one("#settings-model", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        del event
        self.dismiss(
            (
                self.query_one("#settings-model", Input).value.strip(),
                self.query_one("#settings-reasoning", Input).value.strip(),
            )
        )


class SpawnDirectoryScreen(ModalScreen[str | None]):
    """Collect and validate an optional launch directory with native completion."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", priority=True)]

    DEFAULT_CSS = """
    SpawnDirectoryScreen { align: center middle; }
    #spawn-directory { width: 84; height: auto; padding: 1 2; border: solid $accent; }
    #spawn-cwd-help { color: $text-muted; }
    #spawn-cwd-error { color: $error; height: 1; }
    """

    def __init__(self, harness: str, *, base_dir: Path) -> None:
        super().__init__()
        self._harness = harness
        self._base_dir = base_dir

    def compose(self) -> ComposeResult:
        with Vertical(id="spawn-directory"):
            yield Label(f"Spawn {self._harness} in directory")
            yield DirectoryInput(
                value=str(self._base_dir),
                base_dir=self._base_dir,
                id="spawn-cwd",
            )
            yield Label(
                "Tab or → completes directories; relative paths and ~ are supported",
                id="spawn-cwd-help",
            )
            yield Label("", id="spawn-cwd-error", markup=False)

    def on_mount(self) -> None:
        self.query_one("#spawn-cwd", DirectoryInput).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "spawn-cwd":
            self.query_one("#spawn-cwd-error", Label).update("")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "spawn-cwd":
            return
        try:
            cwd = normalize_directory(event.value, base_dir=self._base_dir)
        except ValueError as exc:
            self.query_one("#spawn-cwd-error", Label).update(str(exc))
            return
        self.dismiss(cwd)


class TranscriptTransferScreen(ModalScreen[str | None]):
    """Require the exact prior owner ID before transferring a transcript."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", priority=True)]

    DEFAULT_CSS = """
    TranscriptTransferScreen { align: center middle; }
    #transcript-transfer { width: 88; height: auto; padding: 1 2; border: solid $warning; }
    #transcript-transfer-error { color: $error; height: 1; }
    """

    def __init__(self, *, location: str, prior_owner_id: str, owner_is_dead: bool) -> None:
        super().__init__()
        self._location = location
        self._prior_owner_id = prior_owner_id
        self._owner_is_dead = owner_is_dead

    def compose(self) -> ComposeResult:
        owner_kind = "dead participant" if self._owner_is_dead else "live participant"
        with Vertical(id="transcript-transfer"):
            yield Label("Transfer transcript identity", markup=False)
            yield Label(
                f"{self._location}\nCurrently owned by {owner_kind} {self._prior_owner_id}.",
                markup=False,
            )
            yield Label(
                "Type the exact prior owner ID to confirm the transfer.",
                markup=False,
            )
            yield Input(placeholder=self._prior_owner_id, id="transcript-transfer-confirmation")
            yield Label("", id="transcript-transfer-error", markup=False)

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.value.strip() != self._prior_owner_id:
            self.query_one("#transcript-transfer-error", Label).update(
                "Confirmation must exactly match the prior owner ID."
            )
            return
        self.dismiss(self._prior_owner_id)


__all__ = [
    "ControlPromptScreen",
    "SettingsPromptScreen",
    "SpawnDirectoryScreen",
    "TranscriptTransferScreen",
]
