"""Command palette entries for the régie.

Textual's palette (ctrl+p) already carries the built-in system commands. This
adds one entry per registered harness so that starting a plain CLI does not
mean leaving the régie for a shell and typing `theater spawn`, plus the view
toggles that have no keybinding of their own — the footer has room for the
keys used in every session, not for the ones set once and left alone.

What these entries spawn is deliberately the least opinionated thing possible:
no prompt, no parent, `manual` approval — which for both harnesses means no
approval flags at all. The result is the CLI as the user would have started it
by hand, except that Theater knows about it. Anything more specific (a prompt,
a worktree, a child of the selected agent) is a different affordance and does
not belong on a one-keystroke list.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

from rich.text import Text
from textual.command import DiscoveryHit, Hit, Hits, Provider

from theater.harness import describe, harness_icon


def _order(rows: list[dict], favourite: str | None) -> list[dict]:
    """Harness rows, favourite first, the rest in the order given.

    Promoting one entry rather than filtering the others: the palette is a
    discovery surface, and a favourite that hid its alternatives would make
    the second harness unreachable from the keyboard.
    """
    promoted = [row for row in rows if row["name"] == favourite]
    return promoted + [row for row in rows if row["name"] != favourite]


def entries(
    harnesses: list[dict] | None = None,
    favourite: str | None = None,
) -> list[tuple[str, str, str]]:
    """(display text, harness name, help text) per harness.

    Takes the harness list rather than reading the registry, because the
    authority on what can be spawned is the daemon that will do the spawning —
    see the `harnesses` RPC. `None` falls back to this process's registry so
    the palette still draws when the daemon call failed.
    """
    rows: list[tuple[str, str, str]] = []
    for row in _order(describe() if harnesses is None else harnesses, favourite):
        if row.get("error"):
            # A plugin that would not load; an entry here could only offer a
            # spawn that cannot happen.
            continue
        if not row.get("installed", True):
            # Listed, not hidden: hiding it looks like Theater cannot drive
            # this harness at all, when the truth is the binary is not on PATH.
            help_text = f"{row['binary']} is not on PATH here — this will fail"
        else:
            help_text = f"Start {row['binary']} here, unparented, with no prompt"
        rows.append((f"{row['icon']} Spawn {row['name']}", row["name"], help_text))
    return rows


class SpawnHarnessCommands(Provider):
    """Offer `Spawn <harness>` for each harness Theater knows how to drive.

    Shown in the second palette pushed by SpawnCommand.
    """

    def _hit_command(self, name: str):
        return partial(self.app.spawn_harness, name)  # type: ignore[attr-defined]

    def _favourite(self) -> str | None:
        settings = getattr(self.app, "settings", None)
        if settings is None:
            return None
        return settings.theater.favourite

    def _entries(self) -> list[tuple[str, str, str]]:
        return entries(getattr(self.app, "harnesses", None), self._favourite())

    async def discover(self) -> Hits:
        for display, name, help_text in self._entries():
            yield DiscoveryHit(display, self._hit_command(name), help=help_text)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for display, name, help_text in self._entries():
            score = matcher.match(display)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(display),
                    self._hit_command(name),
                    help=help_text,
                )


class SpawnCommand(Provider):
    """Single 'Spawn' entry that opens a second palette with harness choices."""

    async def discover(self) -> Hits:
        callback = getattr(self.app, "action_spawn", None)
        if callback is None:
            return
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
    """Show and hide the parts of the régie that can be turned off.

    The entry is named for what it will do, not for what it controls: an
    entry reading "Bus panel" would leave the user to guess which way the
    switch is about to move, and the palette closes before they find out.
    """

    def _toggle(self):
        # Same reason as SpawnHarnessCommands._favourite: the app is not always a RegieApp.
        return getattr(self.app, "action_toggle_bus", None)

    def _entry(self) -> tuple[str, str]:
        if getattr(self.app, "bus_visible", True):
            return (
                "Hide bus panel",
                "Give the whole sidebar to the tree; the log stops consuming events",
            )
        return (
            "Show bus panel",
            "Bring the event log back, resuming where it left off",
        )

    async def discover(self) -> Hits:
        toggle = self._toggle()
        if toggle is None:
            return
        display, help_text = self._entry()
        yield DiscoveryHit(display, toggle, help=help_text)

    async def search(self, query: str) -> Hits:
        toggle = self._toggle()
        if toggle is None:
            return
        matcher = self.matcher(query)
        display, help_text = self._entry()
        score = matcher.match(display)
        if score > 0:
            yield Hit(score, matcher.highlight(display), toggle, help=help_text)


def _dead_session_label(row: dict) -> str:
    icon = harness_icon(row.get("harness"))
    harness = row.get("harness", "?")
    cwd = row.get("cwd") or ""
    label = f"{icon} {harness} {cwd}"
    prompt = row.get("spawn_prompt")
    if prompt:
        plain = " ".join(prompt.split())
        if len(plain) > 120:
            plain = plain[:119] + "\u2026"
        label += f"\n{plain}"
    else:
        label += "\n\u00a0"
    return label


def _to_text(display: str) -> Text:
    rendered = Text(display)
    if "\n" in display:
        rendered.stylize("dim", display.index("\n") + 1)
    return rendered


class ResumeDeadSessionCommands(Provider):
    """Lists dead sessions in a second palette (pushed by ResumeDeadSessionCommand)."""

    def __init__(self, screen, match_style=None):
        super().__init__(screen, match_style)
        self.rows: list[dict] = []

    async def startup(self) -> None:
        loader = getattr(self.app, "load_dead_sessions", None)
        if loader is not None:
            try:
                self.rows = await loader()
            except Exception:
                self.rows = []

    @property
    def commands(self) -> list[tuple[str, Callable[[], None]]]:
        callback = getattr(self.app, "resume_dead_session", None)
        if callback is None:
            return []
        return [(_dead_session_label(row), partial(callback, row)) for row in self.rows]

    async def discover(self) -> Hits:
        for display, command in self.commands:
            yield DiscoveryHit(_to_text(display), command, text=display)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for display, command in self.commands:
            score = matcher.match(display)
            if score > 0:
                highlighted = matcher.highlight(display)
                if "\n" in display:
                    highlighted.stylize("dim", display.index("\n") + 1)
                yield Hit(score, highlighted, command, text=display)


class ResumeDeadSessionCommand(Provider):
    """Single 'Resume dead session' entry that opens a second palette."""

    async def discover(self) -> Hits:
        callback = getattr(self.app, "action_resume_dead_session", None)
        if callback is None:
            return
        yield DiscoveryHit(
            "Resume sessions",
            callback,
            help="Browse recent dead sessions and resume one",
        )

    async def search(self, query: str) -> Hits:
        callback = getattr(self.app, "action_resume_dead_session", None)
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
