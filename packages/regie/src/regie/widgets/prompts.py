"""Small public-action dialogs for Régie controls and catalog-backed spawn."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label

from regie.palette import SpawnChoice
from regie.resume import ResumeCandidate


class ControlPromptScreen(ModalScreen[str | None]):
    """Collect one bounded prompt; policy remains with the public daemon API."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", priority=True)]

    DEFAULT_CSS = """
    ControlPromptScreen { align: center middle; }
    #control-prompt { width: 64; height: auto; padding: 1 2; border: solid $accent; }
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
    #settings-prompt { width: 64; height: auto; padding: 1 2; border: solid $accent; }
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


@dataclass(frozen=True, slots=True)
class SpawnRequest:
    """A user-selected catalog launch request, keyed by harness rather than display text."""

    harness: str
    prompt: str
    approval: str


class SpawnPromptScreen(ModalScreen[SpawnRequest | None]):
    """Use daemon-reported harness availability instead of local discovery."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", priority=True)]

    DEFAULT_CSS = """
    SpawnPromptScreen { align: center middle; }
    #spawn-prompt { width: 72; height: auto; padding: 1 2; border: solid $accent; }
    """

    def __init__(self, choices: tuple[SpawnChoice, ...], *, harness: str = "") -> None:
        super().__init__()
        self._choices = choices
        self._harness = harness

    def compose(self) -> ComposeResult:
        available = ", ".join(choice.harness for choice in self._choices if choice.enabled)
        unavailable = ", ".join(
            f"{choice.harness} ({choice.reason or 'unavailable'})"
            for choice in self._choices
            if not choice.enabled
        )
        with Vertical(id="spawn-prompt"):
            yield Label("Spawn from public catalog")
            yield Label(f"available: {available or 'none'}", id="spawn-catalog")
            if unavailable:
                yield Label(f"unavailable: {unavailable}")
            yield Input(value=self._harness, placeholder="harness", id="spawn-harness")
            yield Input(placeholder="initial prompt (optional)", id="spawn-prompt-input")
            yield Input(value="manual", placeholder="approval", id="spawn-approval")

    def on_mount(self) -> None:
        self.query_one("#spawn-harness", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        del event
        harness = self.query_one("#spawn-harness", Input).value.strip()
        prompt = self.query_one("#spawn-prompt-input", Input).value.strip()
        approval = self.query_one("#spawn-approval", Input).value.strip()
        if not harness or not approval:
            return
        self.dismiss(SpawnRequest(harness, prompt, approval))


@dataclass(frozen=True, slots=True)
class ResumeRequest:
    """A deliberate public resume launch, keyed by the historical participant ID."""

    participant_id: str
    prompt: str
    approval: str


class ResumePromptScreen(ModalScreen[ResumeRequest | None]):
    """Choose a bounded, publicly discoverable dead session to resume."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", priority=True)]

    DEFAULT_CSS = """
    ResumePromptScreen { align: center middle; }
    #resume-prompt { width: 84; height: auto; padding: 1 2; border: solid $accent; }
    #resume-candidates { max-height: 12; }
    """

    def __init__(
        self,
        candidates: tuple[ResumeCandidate, ...],
        *,
        more_available: bool,
    ) -> None:
        super().__init__()
        self._candidates = candidates
        self._more_available = more_available

    def compose(self) -> ComposeResult:
        with Vertical(id="resume-prompt"):
            yield Label("Resume a trusted dead session")
            yield Label(self._candidate_text(), id="resume-candidates")
            yield Input(placeholder="historical participant ID", id="resume-participant-id")
            yield Input(
                value="Resume the trusted prior session.",
                placeholder="resume prompt",
                id="resume-prompt-input",
            )
            yield Input(value="manual", placeholder="approval", id="resume-approval")

    def on_mount(self) -> None:
        self.query_one("#resume-participant-id", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        del event
        participant_id = self.query_one("#resume-participant-id", Input).value.strip()
        prompt = self.query_one("#resume-prompt-input", Input).value.strip()
        approval = self.query_one("#resume-approval", Input).value.strip()
        if participant_id and approval:
            self.dismiss(ResumeRequest(participant_id, prompt, approval))

    def _candidate_text(self) -> str:
        if not self._candidates:
            return "No dead sessions were returned by the public API."
        lines = [_candidate_line(candidate) for candidate in self._candidates]
        if self._more_available:
            lines.append("More dead sessions exist; this public discovery page is bounded.")
        return "\n".join(lines)


class PaletteScreen(ModalScreen[str | None]):
    """A minimal command palette whose launch choices come only from the catalog."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", priority=True)]

    DEFAULT_CSS = """
    PaletteScreen { align: center middle; }
    #regie-palette { width: 72; height: auto; padding: 1 2; border: solid $accent; }
    """

    def __init__(self, choices: tuple[SpawnChoice, ...]) -> None:
        super().__init__()
        self._choices = choices

    def compose(self) -> ComposeResult:
        names = ", ".join(choice.harness for choice in self._choices if choice.enabled)
        with Vertical(id="regie-palette"):
            yield Label(
                "Commands: spawn [harness], resume, steer, followup, bus, trajectory, return"
            )
            yield Label(f"spawn catalog: {names or 'none'}")
            yield Input(placeholder="command", id="palette-input")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)


__all__ = [
    "ControlPromptScreen",
    "PaletteScreen",
    "ResumePromptScreen",
    "ResumeRequest",
    "SettingsPromptScreen",
    "SpawnPromptScreen",
    "SpawnRequest",
]


def _candidate_line(candidate: ResumeCandidate) -> str:
    state = "resumable" if candidate.available else candidate.reason or "unavailable"
    cwd = candidate.cwd or "no cwd"
    return f"{candidate.participant_id} · {candidate.harness} · {_clip(cwd)} · {state}"


def _clip(value: str, *, limit: int = 160) -> str:
    return value if len(value) <= limit else f"{value[: limit - 1]}…"
