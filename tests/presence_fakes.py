"""Explicit PresenceProvider double for await, MCP, and régie tests."""

from __future__ import annotations

import asyncio
import time

from theater.daemon.presence.contracts import PresenceSnapshot, PresenceState
from theater.models import HumanPresent

ABSENT = PresenceState.ABSENT
PRESENT = PresenceState.PRESENT
UNKNOWN = PresenceState.UNKNOWN


class FakePresence:
    """Scriptable provider: per-participant states with revision-based wakes."""

    def __init__(self, states: dict[str, PresenceState] | None = None):
        self._states: dict[str, PresenceState] = dict(states or {})
        self._revision = 0
        self._waiters: set[asyncio.Event] = set()
        self.refresh_calls = 0

    def set(self, participant_id: str, state: PresenceState, reason: str = "test") -> None:
        """Flip one participant's presence and wake every waiter."""
        self._states[participant_id] = state
        self._revision += 1
        waiters, self._waiters = self._waiters, set()
        for event in waiters:
            event.set()

    @property
    def revision(self) -> int:
        return self._revision

    def snapshot(self, participant_id: str) -> PresenceSnapshot:
        state = self._states.get(participant_id, ABSENT)
        focus_reason = f"{state} ({participant_id})"
        reason = "no focus" if state is ABSENT else focus_reason
        return PresenceSnapshot(
            state=state,
            reason=reason,
            revision=self._revision,
            observed_at=time.time(),
        )

    async def refresh(self) -> None:
        self.refresh_calls += 1

    async def require_absent(self, participant_id: str) -> None:
        if self.snapshot(participant_id).protected:
            raise HumanPresent(f"human present at {participant_id}; await its release")

    async def wait_for_change(self, after_revision: int) -> int:
        if self._revision > after_revision:
            return self._revision
        event = asyncio.Event()
        self._waiters.add(event)
        try:
            await event.wait()
        finally:
            self._waiters.discard(event)
        return self._revision
