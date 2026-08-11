"""Harness registry.

One module per harness, one instance each. The instances are stateless apart
from the transcript root they read, which is constructor-injected so tests can
point them at a temporary directory instead of the user's real ~/.claude.

Adding a harness is: write the module, add it here. Nothing above this package
needs to change, because nothing above it sees anything but `Event`.
"""

from __future__ import annotations

from pathlib import Path

from theater.harness.base import (
    APPROVALS,
    MAX_TEXT,
    SERVER_NAME,
    Event,
    EventKind,
    Harness,
    LaunchPlan,
    NativeChild,
    clip,
    status_after,
)
from theater.harness.claude_code import ClaudeCodeHarness
from theater.harness.vibe import VibeHarness
from theater.models import BadRequest

HARNESSES: dict[str, Harness] = {
    h.name: h for h in (ClaudeCodeHarness(), VibeHarness())
}


def get(name: str) -> Harness:
    harness = HARNESSES.get(name)
    if harness is None:
        known = ", ".join(sorted(HARNESSES))
        raise BadRequest(f"unknown harness {name!r}; known: {known}")
    return harness


def plan_launch(
    harness: str,
    *,
    participant_id: str,
    prompt: str,
    config_path: Path,
    approval: str,
) -> LaunchPlan:
    return get(harness).plan_launch(
        participant_id=participant_id,
        prompt=prompt,
        config_path=config_path,
        approval=approval,
    )


__all__ = [
    "APPROVALS",
    "HARNESSES",
    "MAX_TEXT",
    "SERVER_NAME",
    "ClaudeCodeHarness",
    "Event",
    "EventKind",
    "Harness",
    "LaunchPlan",
    "NativeChild",
    "VibeHarness",
    "clip",
    "get",
    "plan_launch",
    "status_after",
]
