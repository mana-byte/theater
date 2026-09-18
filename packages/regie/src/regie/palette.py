"""Public-catalog commands for Régie's lightweight command palette."""

from __future__ import annotations

from dataclasses import dataclass

from theater.frontend.dto.catalogs import HarnessCatalogEntry


@dataclass(frozen=True, slots=True)
class SpawnChoice:
    harness: str
    enabled: bool
    reason: str | None = None


def spawn_choices(entries: tuple[HarnessCatalogEntry, ...]) -> tuple[SpawnChoice, ...]:
    """Keep installed-but-unavailable harnesses visible with daemon-provided reasons."""
    return tuple(
        SpawnChoice(entry.name, entry.launch_available, entry.reason or entry.detail)
        for entry in entries
        if entry.installed
    )


__all__ = ["SpawnChoice", "spawn_choices"]
