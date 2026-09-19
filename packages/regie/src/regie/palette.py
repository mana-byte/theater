"""RC9-style command palette entries backed by public catalog state."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

from rich.text import Text
from textual.command import DiscoveryHit, Hit, Hits, Provider

from regie.formatting import harness_icon
from regie.resume import ResumeCandidate, discover_resume_sessions
from theater.frontend.dto.catalogs import HarnessCatalogEntry


@dataclass(frozen=True, slots=True)
class SpawnChoice:
    harness: str
    enabled: bool
    reason: str | None = None
    approvals: tuple[str, ...] | None = None


def spawn_choices(entries: tuple[HarnessCatalogEntry, ...]) -> tuple[SpawnChoice, ...]:
    """Keep every advertised harness discoverable with its public refusal reason."""
    return tuple(
        SpawnChoice(
            entry.name,
            entry.launch_available,
            entry.reason or entry.detail,
            entry.approvals,
        )
        for entry in entries
    )


def spawn_approval(choice: SpawnChoice) -> str | None:
    """Choose RC9's safe palette policy from the daemon-advertised set."""
    approvals = choice.approvals
    if approvals is None:
        return None
    if "manual" in approvals:
        return "manual"
    if len(approvals) == 1:
        return approvals[0]
    return None


class SpawnHarnessCommands(Provider):
    """Offer one harness in the second, spawn-only palette."""

    def _entries(self) -> tuple[tuple[str, str, str, bool], ...]:
        choices = getattr(self.app, "_spawn_choices", lambda: ())()
        entries: list[tuple[str, str, str, bool]] = []
        for choice in choices:
            entries.append(
                (
                    f"{harness_icon(choice.harness)} Spawn {choice.harness}",
                    choice.harness,
                    (
                        f"Start {choice.harness} here, unparented, with no prompt"
                        if choice.enabled
                        else choice.reason or "launch is currently unavailable"
                    ),
                    False,
                )
            )
            if choice.enabled:
                entries.append(
                    (
                        f"{harness_icon(choice.harness)} Spawn {choice.harness} in directory…",
                        choice.harness,
                        "Choose a working directory with filesystem completion",
                        True,
                    )
                )
        return tuple(entries)

    def _command(self, harness: str, choose_directory: bool) -> Callable[[], None]:
        method = "spawn_harness_in_directory" if choose_directory else "spawn_harness"
        return partial(getattr(self.app, method), harness)

    async def discover(self) -> Hits:
        for display, harness, help_text, choose_directory in self._entries():
            yield DiscoveryHit(
                display,
                self._command(harness, choose_directory),
                help=help_text,
            )

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for display, harness, help_text, choose_directory in self._entries():
            score = matcher.match(display)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(display),
                    self._command(harness, choose_directory),
                    help=help_text,
                )


class SpawnCommand(Provider):
    """Keep one Spawn command in the root palette, as in RC9."""

    async def discover(self) -> Hits:
        callback = getattr(self.app, "action_spawn", None)
        if callback is not None:
            yield DiscoveryHit(
                "Spawn",
                callback,
                help="Spawn a fresh session from a supported harness",
            )

    async def search(self, query: str) -> Hits:
        callback = getattr(self.app, "action_spawn", None)
        if callback is None:
            return
        matcher = self.matcher(query)
        display = "Spawn"
        score = matcher.match(display)
        if score > 0:
            yield Hit(
                score,
                matcher.highlight(display),
                callback,
                help="Spawn a fresh session from a supported harness",
            )


class ViewCommands(Provider):
    """Expose only the state-aware bus toggle from RC9's root palette."""

    def _entry(self) -> tuple[str, str]:
        if getattr(self.app, "bus_visible", True):
            return (
                "Hide bus panel",
                "Give the whole sidebar to the tree; the log stops consuming events",
            )
        return ("Show bus panel", "Bring the event log back, resuming where it left off")

    async def discover(self) -> Hits:
        callback = getattr(self.app, "action_toggle_bus", None)
        if callback is not None:
            display, help_text = self._entry()
            yield DiscoveryHit(display, callback, help=help_text)

    async def search(self, query: str) -> Hits:
        callback = getattr(self.app, "action_toggle_bus", None)
        if callback is None:
            return
        display, help_text = self._entry()
        matcher = self.matcher(query)
        score = matcher.match(display)
        if score > 0:
            yield Hit(score, matcher.highlight(display), callback, help=help_text)


class ResumeSessionCommands(Provider):
    """Load one bounded public page and expose each historical session."""

    def __init__(self, screen, match_style=None) -> None:
        super().__init__(screen, match_style)
        self._candidates: tuple[ResumeCandidate, ...] = ()

    async def startup(self) -> None:
        try:
            discovery = await discover_resume_sessions(self.app.client)  # type: ignore[attr-defined]
        except Exception:
            self._candidates = ()
        else:
            self._candidates = discovery.candidates

    @staticmethod
    def _display(candidate: ResumeCandidate) -> str:
        cwd = candidate.cwd or ""
        label = f"{harness_icon(candidate.harness)} {candidate.harness} {cwd}"
        context = " ".join((candidate.description or "").split())
        if len(context) > 120:
            context = f"{context[:119]}…"
        return f"{label}\n{context or chr(160)}"

    @staticmethod
    def _search_text(candidate: ResumeCandidate, display: str) -> str:
        return "\n".join(
            value for value in (display, candidate.cwd, candidate.participant_id) if value
        )

    def _command(self, candidate: ResumeCandidate) -> Callable[[], None]:
        return partial(self.app.resume_dead_session, candidate)  # type: ignore[attr-defined]

    @staticmethod
    def _render(display: str) -> Text:
        rendered = Text(display)
        rendered.stylize("dim", display.index("\n") + 1)
        return rendered

    async def discover(self) -> Hits:
        for candidate in self._candidates:
            display = self._display(candidate)
            search_text = self._search_text(candidate, display)
            yield DiscoveryHit(
                self._render(display),
                self._command(candidate),
                help=candidate.reason,
                text=search_text,
            )

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for candidate in self._candidates:
            display = self._display(candidate)
            search_text = self._search_text(candidate, display)
            score = matcher.match(search_text)
            if score > 0:
                highlighted = matcher.highlight(display)
                highlighted.stylize("dim", display.index("\n") + 1)
                yield Hit(
                    score,
                    highlighted,
                    self._command(candidate),
                    help=candidate.reason,
                    text=search_text,
                )


class ResumeSessionCommand(Provider):
    """Keep one Resume command in the root palette, as in RC9."""

    async def discover(self) -> Hits:
        callback = getattr(self.app, "action_resume_palette", None)
        if callback is not None:
            yield DiscoveryHit(
                "Resume sessions",
                callback,
                help="Browse recent dead sessions and resume one",
            )

    async def search(self, query: str) -> Hits:
        callback = getattr(self.app, "action_resume_palette", None)
        if callback is None:
            return
        matcher = self.matcher(query)
        display = "Resume sessions"
        score = matcher.match(display)
        if score > 0:
            yield Hit(
                score,
                matcher.highlight(display),
                callback,
                help="Browse recent dead sessions and resume one",
            )


__all__ = [
    "ResumeSessionCommand",
    "ResumeSessionCommands",
    "SpawnChoice",
    "SpawnCommand",
    "SpawnHarnessCommands",
    "ViewCommands",
    "spawn_approval",
    "spawn_choices",
]
