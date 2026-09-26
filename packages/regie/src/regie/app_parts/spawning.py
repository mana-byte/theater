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
from regie.widgets import ParticipantTree
from regie.widgets.directory_input import normalize_directory
from regie.widgets.spawn_leaf import SpawnLeaf


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
        """Show the new agent's row in the tree and ask for its directory on it."""
        approval = self._spawn_approval(harness)
        if approval is None:
            return
        if self._state.projection is None:
            self.notify("orchestration state has not loaded yet", severity="warning")
            return
        self._pending_spawn = {
            "harness": harness,
            "icon": self.icon_for_harness(harness),
            "status": "idle",
            "name": "new agent",
            "approval": approval,
        }
        self._show_projection(self._state.projection)
        self.call_after_refresh(self._begin_spawn_directory)

    def _begin_spawn_directory(self) -> None:
        row = self.query_one(ParticipantTree)._key_widgets.get(("n", "new"))
        if isinstance(row, SpawnLeaf):
            self.run_worker(row.begin_rename(), exclusive=False)
        else:
            self.cancel_pending_spawn()

    def submit_pending_spawn(self, value: str) -> str | None:
        """Launch the pending spawn in *value*; return why not, keeping the row open."""
        pending = self._pending_spawn
        if pending is None:
            return None
        try:
            cwd = normalize_directory(value, base_dir=Path.cwd())
        except ValueError as exc:
            return str(exc)
        self.cancel_pending_spawn()
        harness, approval = str(pending["harness"]), str(pending["approval"])
        self._start_action(self.submit_spawn(harness, "", approval, cwd=cwd))
        return None

    def cancel_pending_spawn(self) -> None:
        if self._pending_spawn is None:
            return
        self._pending_spawn = None
        if (projection := self._state.projection) is not None:
            self._show_projection(projection)
        self._restore_tree_focus()

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

    def resume_dead_session(self, candidate: ResumeCandidate) -> None:
        """Resume the trusted session selected in the RC9-style palette."""
        if not candidate.available:
            self.notify(candidate.reason or "session cannot be resumed", severity="warning")
            return
        approval = self._spawn_approval(candidate.harness)
        if approval is not None:
            self._start_action(self.submit_resume(candidate, "", approval))
