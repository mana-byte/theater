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
    theater_binary,
)
from theater.harness.claude_code import ClaudeCodeHarness
from theater.harness.vibe import VibeHarness
from theater.models import BadRequest

HARNESSES: dict[str, Harness] = {
    h.name: h for h in (ClaudeCodeHarness(), VibeHarness())
}

#: Aliases that a misreporting agent might send at registration. The canonical
#: name is what the observer needs to match it to a harness adapter; without
#: normalization, `claude_code` or `Claude` registers happily and is then
#: unobservable forever, because the observer looks up `HARNESSES[name]` and
#: misses.
_ALIASES: dict[str, str] = {
    "claude_code": "claude",
    "claude-code": "claude",
    "Claude": "claude",
    "ClaudeCode": "claude",
    "vibe": "vibe",
    "Vibe": "vibe",
    "mistral-vibe": "vibe",
    "mistral_vibe": "vibe",
}


def normalize(name: str) -> str:
    """Map a harness name as an agent might report it to the canonical key.

    Unknown names are returned unchanged so the caller can decide whether to
    reject or accept as-is — `register` accepts and warns, because a genuinely
    unknown harness is not an error at first contact, just an unobservable one.
    """
    return _ALIASES.get(name, name)


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


#: Shown for a participant whose harness has no adapter — an unmanaged pane,
#: or an agent that registered under a name we do not recognise.
UNKNOWN_ICON = "?"


def harness_icon(name: str | None) -> str:
    """The one-character mark for a harness name, as reported by a participant.

    Normalizes first, so an agent that registered as `claude-code` still gets
    the Claude glyph. Unknown names are not an error here: an external
    participant may be running something Theater has never heard of, and a
    listing should say so rather than refuse to draw the row.
    """
    harness = HARNESSES.get(normalize(name or ""))
    return harness.icon if harness else UNKNOWN_ICON


def known_binaries() -> set[str]:
    """Every binary name the registered harnesses look for on PATH.

    Used by the unmanaged-pane sweep: a pane whose current command matches one
    of these is a harness the daemon can observe if only it knew the session,
    so it should be surfaced rather than invisible.
    """
    return {h.binary for h in HARNESSES.values()}


__all__ = [
    "APPROVALS",
    "HARNESSES",
    "MAX_TEXT",
    "SERVER_NAME",
    "UNKNOWN_ICON",
    "ClaudeCodeHarness",
    "Event",
    "EventKind",
    "Harness",
    "LaunchPlan",
    "NativeChild",
    "VibeHarness",
    "clip",
    "get",
    "harness_icon",
    "known_binaries",
    "normalize",
    "plan_launch",
    "status_after",
    "theater_binary",
]
