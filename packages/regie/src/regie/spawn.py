"""Which harnesses can be spawned, in what order, and under which approval."""

from __future__ import annotations

from dataclasses import dataclass

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


__all__ = ["SpawnChoice", "spawn_approval", "spawn_choices"]
