"""Public presence observation seam; bridge-owned tracking lives in focus_monitor."""

from __future__ import annotations

from typing import Protocol

from regie.tmux.command import TmuxError
from regie.tmux.focus_facts import read_inventory
from regie.tmux.focus_policy import FocusTrust, PresenceEvidence, classify
from regie.tmux.identity import PaneSnapshot


class PresenceObserver(Protocol):
    """``requested_at`` (monotonic) lets concurrent callers share one read begun after it."""

    async def __call__(
        self, expected: PaneSnapshot, *, requested_at: float | None = None
    ) -> PresenceEvidence: ...


class PresenceChanged(TmuxError):
    """Fresh presence admission was revoked before a terminal effect."""


async def observe_presence(
    expected: PaneSnapshot, *, requested_at: float | None = None
) -> PresenceEvidence:
    """A one-shot read can protect focus, but cannot prove a client's blur history."""
    return classify(expected, await read_inventory(expected.server_identity), FocusTrust())


__all__ = ["PresenceChanged", "PresenceEvidence", "PresenceObserver", "observe_presence"]
