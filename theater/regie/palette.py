"""Command palette entries for the régie.

Textual's palette (ctrl+p) already carries the built-in system commands. This
adds one entry per registered harness so that starting a plain CLI does not
mean leaving the régie for a shell and typing `theater spawn`.

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
            # A plugin that would not load. `theater harnesses` says so with
            # the parse error; an entry here could only offer a spawn that
            # cannot happen.
            continue
        if not row.get("installed", True):
            # Listed anyway. Hiding the entry looks like Theater cannot drive
            # this harness at all, when the truth is narrower and fixable:
            # the binary is not on PATH on this machine.
            help_text = f"{row['binary']} is not on PATH here — this will fail"
        else:
            help_text = f"Start {row['binary']} here, unparented, with no prompt"
        rows.append((f"{row['icon']} Spawn {row['name']}", row["name"], help_text))
    return rows


class SpawnCommands(Provider):
    """Offer `Spawn <harness>` for each harness Theater knows how to drive."""

    def _hit_command(self, name: str):
        # partial, not a closure over the loop variable: every hit would
        # otherwise spawn whichever harness the loop happened to end on.
        return partial(self.app.spawn_harness, name)  # type: ignore[attr-defined]

    def _favourite(self) -> str | None:
        # Defensive: Provider is constructed by Textual against whatever app is
        # running, and the palette must not be the thing that breaks a screen.
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
