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

from functools import partial

from textual.command import DiscoveryHit, Hit, Hits, Provider

from theater.harness import describe


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


class SpawnCommands(Provider):
    """Offer `Spawn <harness>` for each harness Theater knows how to drive."""

    def _hit_command(self, name: str):
        # partial, not a closure over the loop variable: a closure would
        # spawn whichever harness the loop ended on.
        return partial(self.app.spawn_harness, name)  # type: ignore[attr-defined]

    def _favourite(self) -> str | None:
        # Defensive: Textual builds providers against whatever app is running;
        # the palette must not break a screen.
        settings = getattr(self.app, "settings", None)
        if settings is None:
            return None
        return settings.theater.favourite

    def _entries(self) -> list[tuple[str, str, str]]:
        return entries(getattr(self.app, "harnesses", None), self._favourite())

    async def discover(self) -> Hits:
        """Shown before the user types anything, which is where these belong."""
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


class ViewCommands(Provider):
    """Show and hide the parts of the régie that can be turned off.

    The entry is named for what it will do, not for what it controls: an
    entry reading "Bus panel" would leave the user to guess which way the
    switch is about to move, and the palette closes before they find out.
    """

    def _toggle(self):
        # Same reason as SpawnCommands._favourite: the app is not always a RegieApp.
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
