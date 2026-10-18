"""Presence-provider doubles: per-participant scripted, and fixed-state absent/present/unknown."""

from __future__ import annotations

import asyncio
import time

import pytest

from theater.daemon.presence.contracts import PresenceSnapshot, PresenceState
from theater.models import HumanPresent

ABSENT = PresenceState.ABSENT
PRESENT = PresenceState.PRESENT
UNKNOWN = PresenceState.UNKNOWN


class FakePresence:
    """Scriptable provider: per-participant states with revision-based wakes."""

    def __init__(self, states: dict[str, PresenceState] | None = None, capture_screen=None):
        self._states: dict[str, PresenceState] = dict(states or {})
        self._capture_screen = capture_screen
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

    async def capture_screen(self, participant_id: str, *, max_bytes: int):
        """Delegate to the supplied real monitor; no evidence without one."""
        if self._capture_screen is None:
            return None
        return await self._capture_screen(participant_id, max_bytes=max_bytes)

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


class _BasePresence:
    """Shared plumbing: revision tracking and change wakeups."""

    def __init__(self, state: PresenceState, reason: str, capture_screen=None) -> None:
        self.state = state
        self.reason = reason
        self.revision = 1
        self.observed_at = 1.0
        self.absence_checks: list[str] = []
        self.refreshes = 0
        self._capture_screen = capture_screen
        self._changed = asyncio.Event()

    @property
    def revision(self) -> int:
        return self._revision

    @revision.setter
    def revision(self, value: int) -> None:
        self._revision = value

    def snapshot(self, participant_id: str) -> PresenceSnapshot:
        return PresenceSnapshot(self.state, self.reason, self._revision, self.observed_at)

    async def refresh(self) -> None:
        self.refreshes += 1

    async def capture_screen(self, participant_id: str, *, max_bytes: int):
        """Delegate to the supplied real monitor; no evidence without one."""
        if self._capture_screen is None:
            return None
        return await self._capture_screen(participant_id, max_bytes=max_bytes)

    async def start(self) -> None:
        await self.refresh()

    async def reconcile(self) -> None:
        await self.refresh()

    async def aclose(self) -> None:
        pass

    async def require_absent(self, participant_id: str) -> None:
        self.absence_checks.append(participant_id)
        await self.refresh()
        if self.snapshot(participant_id).protected:
            raise HumanPresent(
                f"human focus protects participant {participant_id!r}; "
                "the caller must wait for the human to leave before acting"
            )

    async def wait_for_change(self, after_revision: int) -> int:
        while self._revision <= after_revision:
            await self._changed.wait()
        return self._revision

    def set_state(self, state: PresenceState, reason: str) -> None:
        self.state = state
        self.reason = reason
        self._revision += 1
        old, self._changed = self._changed, asyncio.Event()
        old.set()


class AbsentPresence(_BasePresence):
    """No human ever has focus: every mutation gate passes."""

    def __init__(self, capture_screen=None) -> None:
        super().__init__(
            PresenceState.ABSENT, "test double: no human focus", capture_screen=capture_screen
        )


class PresentPresence(_BasePresence):
    """A human holds focus: every mutation gate refuses."""

    def __init__(self, capture_screen=None) -> None:
        super().__init__(
            PresenceState.PRESENT, "test double: human focus", capture_screen=capture_screen
        )


class UnknownPresence(_BasePresence):
    """Focus facts are missing: protection fails closed."""

    def __init__(self, capture_screen=None) -> None:
        super().__init__(
            PresenceState.UNKNOWN, "test double: unknown focus", capture_screen=capture_screen
        )


class FlipOnRefreshPresence(_BasePresence):
    """Absent until refresh() runs, then flips to the configured state."""

    def __init__(self, state: PresenceState = PresenceState.PRESENT, capture_screen=None) -> None:
        super().__init__(
            PresenceState.ABSENT, "test double: absent so far", capture_screen=capture_screen
        )
        self._flip_state = state

    async def refresh(self) -> None:
        await super().refresh()
        if self.refreshes == 1:
            self.set_state(self._flip_state, "test double: focus arrived")


@pytest.fixture
async def absent_presence(daemon):
    """No human focus: the composed gates pass; opt in per module by importing it autouse."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(daemon, "presence", AbsentPresence(), raising=False)
        yield
