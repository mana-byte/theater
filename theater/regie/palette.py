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

import shutil
from functools import partial

from textual.command import DiscoveryHit, Hit, Hits, Provider

from theater.harness import HARNESSES


def entries() -> list[tuple[str, str, str]]:
    """(display text, harness name, help text) for every registered harness."""
    rows: list[tuple[str, str, str]] = []
    for name in sorted(HARNESSES):
        harness = HARNESSES[name]
        if shutil.which(harness.binary) is None:
            # Listed anyway. Hiding the entry looks like Theater cannot drive
            # this harness at all, when the truth is narrower and fixable:
            # the binary is not on PATH on this machine.
            help_text = f"{harness.binary} is not on PATH here — this will fail"
        else:
            help_text = f"Start {harness.binary} here, unparented, with no prompt"
        rows.append((f"{harness.icon} Spawn {name}", name, help_text))
    return rows


class SpawnCommands(Provider):
    """Offer `Spawn <harness>` for each harness Theater knows how to drive."""

    def _hit_command(self, name: str):
        # partial, not a closure over the loop variable: every hit would
        # otherwise spawn whichever harness the loop happened to end on.
        return partial(self.app.spawn_harness, name)  # type: ignore[attr-defined]

    async def discover(self) -> Hits:
        """Shown before the user types anything, which is where these belong."""
        for display, name, help_text in entries():
            yield DiscoveryHit(display, self._hit_command(name), help=help_text)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for display, name, help_text in entries():
            score = matcher.match(display)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(display),
                    self._hit_command(name),
                    help=help_text,
                )
