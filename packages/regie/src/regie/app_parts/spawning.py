"""Spawn and resume palettes, prompts, and approval choice."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from textual.command import CommandPalette

from regie.app_parts._shared import _AppBase
from regie.palette import (
    ResumeSessionCommands,
    SpawnChoice,
    SpawnHarnessCommands,
    spawn_approval,
    spawn_choices,
)
from regie.resume import ResumeCandidate, ResumeDiscovery, discover_resume_sessions
from regie.widgets.prompts import (
    ResumePromptScreen,
    ResumeRequest,
    SpawnDirectoryScreen,
)
from theater.frontend import (
    FrontendClientError,
    FrontendResponseError,
    FrontendTransportError,
)


class SpawnResume(_AppBase):
    def action_resume_palette(self) -> None:
        self.push_screen(
            CommandPalette(
                providers=[ResumeSessionCommands],
                placeholder="Search for dead sessions…",
            )
        )

    async def load_resume_sessions(self) -> ResumeDiscovery:
        """Load palette candidates on its isolated ordinary-request connection."""
        async with self._resume_discovery_lock:
            discovery = await discover_resume_sessions(self._clients.resume)
        if not self._harnesses:  # catalog not loaded yet: nothing to judge by
            return discovery
        installed = {entry.name for entry in self._installed_harnesses}
        return replace(
            discovery,
            candidates=tuple(
                candidate for candidate in discovery.candidates if candidate.harness in installed
            ),
        )

    def _spawn_choices(self) -> tuple[SpawnChoice, ...]:
        return spawn_choices(self._installed_harnesses, self.settings.favourite)

    def icon_for_harness(self, harness: str) -> str | None:
        """Return the daemon-advertised icon for one canonical harness."""
        return next((entry.icon for entry in self._harnesses if entry.name == harness), None)

    def action_spawn(self) -> None:
        self.push_screen(
            CommandPalette(
                providers=[SpawnHarnessCommands],
                placeholder="Spawn a fresh session…",
            )
        )

    def spawn_harness(self, harness: str) -> None:
        """Open a completing directory prompt for an otherwise bare spawn."""
        approval = self._spawn_approval(harness)
        if approval is None:
            return

        def receive(cwd: str | None) -> None:
            if cwd is not None:
                self._start_action(self.submit_spawn(harness, "", approval, cwd=cwd))

        self.push_screen(SpawnDirectoryScreen(harness, base_dir=Path.cwd()), receive)

    def _spawn_approval(self, harness: str) -> str | None:
        choice = next(
            (item for item in self._spawn_choices() if item.harness == harness),
            None,
        )
        if choice is None:
            self.notify("harness is not in the public catalog", severity="warning")
            return None
        if not choice.enabled:
            self.notify(choice.reason or "harness launch is unavailable", severity="warning")
            return None
        approval = spawn_approval(choice)
        if approval is None:
            detail = (
                "approval policies are absent from the public catalog"
                if choice.approvals is None
                else f"no safe automatic choice among {', '.join(choice.approvals) or 'none'}"
            )
            self.notify(f"cannot spawn {harness}: {detail}", severity="warning")
        return approval

    async def action_resume_sessions(self) -> None:
        """List a bounded public dead-session page before any resume mutation is offered."""
        try:
            discovery = await self.load_resume_sessions()
        except (
            FrontendClientError,
            FrontendResponseError,
            FrontendTransportError,
            TypeError,
        ) as exc:
            self.notify(f"resume sessions unavailable: {exc}", severity="warning")
            return
        self._resume_candidates = {
            candidate.participant_id: candidate for candidate in discovery.candidates
        }
        self.push_screen(
            ResumePromptScreen(
                discovery.candidates,
                more_available=discovery.more_available,
            ),
            self._submit_resume_request,
        )

    def resume_dead_session(self, candidate: ResumeCandidate) -> None:
        """Resume the trusted session selected in the RC9-style palette."""
        if not candidate.available:
            self.notify(candidate.reason or "session cannot be resumed", severity="warning")
            return
        approval = self._spawn_approval(candidate.harness)
        if approval is not None:
            self._start_action(self.submit_resume(candidate, "", approval))

    def _submit_resume_request(self, request: ResumeRequest | None) -> None:
        if request is None:
            return
        candidate = self._resume_candidates.get(request.participant_id)
        if candidate is None:
            self.notify("session is not in the bounded public resume list", severity="warning")
            return
        if not candidate.available:
            self.notify(candidate.reason or "session cannot be resumed", severity="warning")
            return
        if request.approval not in {"manual", "edits", "yolo"}:
            self.notify("approval must be manual, edits, or yolo", severity="warning")
            return
        self._start_action(self.submit_resume(candidate, request.prompt, request.approval))
