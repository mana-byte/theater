"""RC9-style command palette entries backed by public catalog state."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

from rich.text import Text
from textual.command import DiscoveryHit, Hit, Hits, Provider

from regie.controllers.palette_loading import PaletteLoad
from regie.formatting import harness_icon
from regie.resume import ResumeCandidate, discover_resume_sessions
from theater.frontend.dto import TranscriptCandidate
from theater.frontend.dto.catalogs import HarnessCatalogEntry


@dataclass(frozen=True, slots=True)
class SpawnChoice:
    harness: str
    enabled: bool
    reason: str | None = None
    approvals: tuple[str, ...] | None = None


def spawn_choices(
    entries: tuple[HarnessCatalogEntry, ...], favourite: str | None = None
) -> tuple[SpawnChoice, ...]:
    """Keep every advertised harness discoverable with its public refusal reason."""
    choices = tuple(
        SpawnChoice(
            entry.name,
            enabled=entry.launch_available,
            reason=entry.reason or entry.detail,
            approvals=entry.approvals,
        )
        for entry in entries
    )
    return tuple(choice for choice in choices if choice.harness == favourite) + tuple(
        choice for choice in choices if choice.harness != favourite
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


# One palette command: display text, help text, and what it runs.
Entry = tuple[str, str, Callable[[], object]]


class _SingleCommand(Provider):
    """A provider offering at most one command, described by `_entry`."""

    def _entry(self) -> Entry | None:
        raise NotImplementedError

    async def discover(self) -> Hits:
        entry = self._entry()
        if entry is not None:
            display, help_text, callback = entry
            yield DiscoveryHit(display, callback, help=help_text)

    async def search(self, query: str) -> Hits:
        entry = self._entry()
        if entry is None:
            return
        display, help_text, callback = entry
        matcher = self.matcher(query)
        score = matcher.match(display)
        if score > 0:
            yield Hit(score, matcher.highlight(display), callback, help=help_text)


class SpawnHarnessCommands(Provider):
    """Offer one directory-selecting spawn per public harness."""

    async def _wait_for_catalog(self) -> None:
        # Query cancellation must not poison Textual's one-shot provider startup.
        wait = getattr(self.app, "wait_for_catalog", None)
        if wait is not None:
            await wait()

    def _entries(self) -> tuple[tuple[str, str, str], ...]:
        choices = getattr(self.app, "_spawn_choices", lambda: ())()
        return tuple(
            (
                f"Spawn {choice.harness}",
                choice.harness,
                (
                    "Choose a working directory with filesystem completion"
                    if choice.enabled
                    else choice.reason or "launch is currently unavailable"
                ),
            )
            for choice in choices
        )

    def _command(self, harness: str) -> Callable[[], None]:
        return partial(self.app.spawn_harness, harness)  # type: ignore[attr-defined]

    async def discover(self) -> Hits:
        await self._wait_for_catalog()
        for display, harness, help_text in self._entries():
            yield DiscoveryHit(
                display,
                self._command(harness),
                help=help_text,
            )

    async def search(self, query: str) -> Hits:
        await self._wait_for_catalog()
        matcher = self.matcher(query)
        for display, harness, help_text in self._entries():
            score = matcher.match(display)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(display),
                    self._command(harness),
                    help=help_text,
                )


class SpawnCommand(_SingleCommand):
    """Keep one Spawn command in the root palette, as in RC9."""

    def _entry(self) -> Entry | None:
        callback = getattr(self.app, "action_spawn", None)
        if callback is None:
            return None
        return "Spawn", "Spawn a fresh session from a supported harness", callback


class ViewCommands(_SingleCommand):
    """Expose only the state-aware bus toggle from RC9's root palette."""

    def _entry(self) -> Entry | None:
        callback = getattr(self.app, "action_toggle_bus", None)
        if callback is None:
            return None
        if getattr(self.app, "bus_visible", True):
            return (
                "Hide bus panel",
                "Give the whole sidebar to the tree; the log stops consuming events",
                callback,
            )
        return "Show bus panel", "Bring the event log back, resuming where it left off", callback


class UsageViewCommand(_SingleCommand):
    """Toggle the usage footer; per-agent cost on the selected row stays either way."""

    def _entry(self) -> Entry | None:
        callback = getattr(self.app, "action_toggle_usage", None)
        if callback is None:
            return None
        if getattr(self.app, "usage_visible", False):
            return "Hide usage footer", "Keep costs out of sight; `$` toggles it", callback
        return "Show usage footer", "Show totals for the cost window; `$` toggles it", callback


class RetryActionCommand(_SingleCommand):
    """Offer an explicit replay only for the latest uncertain durable action."""

    def _entry(self) -> Entry | None:
        latest = getattr(self.app, "latest_uncertain_action", None)
        callback = getattr(self.app, "retry_latest_action", None)
        record = latest() if callable(latest) else None
        if record is None or not callable(callback):
            return None
        display = f"Retry uncertain {record.action}"
        return display, "Replay the retained idempotency key for this action", callback


class ResumeSessionCommands(Provider):
    """Load one bounded public page and expose each historical session."""

    def __init__(self, screen, match_style=None) -> None:
        super().__init__(screen, match_style)
        self._candidates: tuple[ResumeCandidate, ...] = ()
        self._load = PaletteLoad(self._load_candidates)

    async def startup(self) -> None:
        self._load.start()

    async def shutdown(self) -> None:
        await self._load.close()

    async def _load_candidates(self) -> None:
        try:
            loader = getattr(self.app, "load_resume_sessions", None)
            discovery = (
                await loader()
                if callable(loader)
                else await discover_resume_sessions(self.app.client)  # type: ignore[attr-defined]
            )
        except Exception:
            self._candidates = ()
        else:
            self._candidates = discovery.candidates

    def _display(self, candidate: ResumeCandidate) -> str:
        cwd = candidate.cwd or ""
        icon_for_harness = getattr(self.app, "icon_for_harness", None)
        icon = icon_for_harness(candidate.harness) if callable(icon_for_harness) else None
        label = f"{icon or harness_icon(candidate.harness)} {candidate.harness} {cwd}"
        context = " ".join((candidate.description or candidate.spawn_prompt or "").split())
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
        await self._load.wait()
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
        await self._load.wait()
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


class ResumeSessionCommand(_SingleCommand):
    """Keep one Resume command in the root palette, as in RC9."""

    def _entry(self) -> Entry | None:
        callback = getattr(self.app, "action_resume_palette", None)
        if callback is None:
            return None
        return "Resume sessions", "Browse recent dead sessions and resume one", callback


class TranscriptCandidateCommands(Provider):
    """Offer daemon-admitted transcript candidates for one stable participant."""

    def __init__(self, screen, match_style=None) -> None:
        super().__init__(screen, match_style)
        self._candidates: tuple[TranscriptCandidate, ...] = ()
        self._load = PaletteLoad(self._load_candidates)

    async def startup(self) -> None:
        self._load.start()

    async def shutdown(self) -> None:
        await self._load.close()

    async def _load_candidates(self) -> None:
        loader = getattr(self.app, "load_transcript_candidates", None)
        if callable(loader):
            self._candidates = await loader()

    @staticmethod
    def _display(candidate: TranscriptCandidate) -> str:
        state = candidate.rejection_reason or candidate.provenance or "unverified"
        owner = candidate.owner_id or candidate.tombstone_id
        ownership = f" · owned by {owner}" if owner else ""
        return f"{candidate.location}\n{state}{ownership}"

    @staticmethod
    def _search_text(candidate: TranscriptCandidate, display: str) -> str:
        return "\n".join(
            value
            for value in (
                display,
                candidate.session_id,
                candidate.domain,
                candidate.owner_id,
                candidate.tombstone_id,
            )
            if value
        )

    def _command(self, candidate: TranscriptCandidate) -> Callable[[], None]:
        participant_id = getattr(self.app, "_transcript_recovery_target", None)
        callback = self.app.select_transcript_candidate  # type: ignore[attr-defined]
        return partial(
            callback,
            candidate,
            participant_id=participant_id,
        )

    @staticmethod
    def _render(display: str) -> Text:
        rendered = Text(display)
        rendered.stylize("dim", display.index("\n") + 1)
        return rendered

    async def discover(self) -> Hits:
        await self._load.wait()
        for candidate in self._candidates:
            display = self._display(candidate)
            yield DiscoveryHit(
                self._render(display),
                self._command(candidate),
                help=candidate.rejection_reason,
                text=self._search_text(candidate, display),
            )

    async def search(self, query: str) -> Hits:
        await self._load.wait()
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
                    help=candidate.rejection_reason,
                    text=search_text,
                )


class TranscriptRecoveryCommand(_SingleCommand):
    """Expose transcript recovery for the currently selected managed participant."""

    def _entry(self) -> Entry | None:
        participant_id = getattr(self.app, "selected_participant_id", None)
        callback = getattr(self.app, "action_recover_transcript", None)
        if not isinstance(participant_id, str) or not participant_id or not callable(callback):
            return None
        available = getattr(self.app, "transcript_recovery_available", None)
        if callable(available) and not available(participant_id):
            return None
        display = f"Recover transcript identity · {participant_id}"
        return display, "Inspect and bind a daemon-admitted transcript candidate", callback


class AddSeparatorCommand(_SingleCommand):
    """Add a named divider above the selected managed tree row."""

    def _entry(self) -> Entry | None:
        callback = getattr(self.app, "action_add_separator", None)
        if callback is None:
            return None
        return "Add separator", "Add a named divider above the selected tree row", callback


__all__ = [
    "AddSeparatorCommand",
    "ResumeSessionCommand",
    "ResumeSessionCommands",
    "RetryActionCommand",
    "SpawnChoice",
    "SpawnCommand",
    "SpawnHarnessCommands",
    "TranscriptCandidateCommands",
    "TranscriptRecoveryCommand",
    "UsageViewCommand",
    "ViewCommands",
    "spawn_approval",
    "spawn_choices",
]
