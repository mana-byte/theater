"""Explicit PresenceProvider doubles shared by the control-surface tests.

Each double implements the frozen ``theater.daemon.presence`` contract so the
daemon-owned gates can be driven deterministically. Production composes the
real monitor; these exist only for tests.
"""

from __future__ import annotations

import asyncio

from theater.daemon.presence import PresenceSnapshot, PresenceState


class _BasePresence:
    """Shared plumbing: revision tracking and change wakeups."""

    def __init__(self, state: PresenceState, reason: str) -> None:
        self.state = state
        self.reason = reason
        self.revision = 1
        self.observed_at = 1.0
        self.absence_checks: list[str] = []
        self.refreshes = 0

    @property
    def revision(self) -> int:
        return self._revision

    @revision.setter
    def revision(self, value: int) -> None:
        self._revision = value

    def snapshot(self, participant_id: str) -> PresenceSnapshot:
        return PresenceSnapshot(
            self.state, self.reason, self._revision, self.observed_at
        )

    async def refresh(self) -> None:
        self.refreshes += 1

    async def require_absent(self, participant_id: str) -> None:
        from theater.models import HumanPresent

        self.absence_checks.append(participant_id)
        await self.refresh()
        if self.snapshot(participant_id).protected:
            raise HumanPresent(
                f"human focus protects participant {participant_id!r}; "
                "the caller must wait for the human to leave before acting"
            )

    async def wait_for_change(self, after_revision: int) -> int:
        while self._revision <= after_revision:
            await asyncio.sleep(0)
        return self._revision

    def set_state(self, state: PresenceState, reason: str) -> None:
        self.state = state
        self.reason = reason
        self._revision += 1


class AbsentPresence(_BasePresence):
    """No human ever has focus: every mutation gate passes."""

    def __init__(self) -> None:
        super().__init__(PresenceState.ABSENT, "test double: no human focus")


class PresentPresence(_BasePresence):
    """A human holds focus: every mutation gate refuses."""

    def __init__(self) -> None:
        super().__init__(PresenceState.PRESENT, "test double: human focus")


class UnknownPresence(_BasePresence):
    """Focus facts are missing: protection fails closed."""

    def __init__(self) -> None:
        super().__init__(PresenceState.UNKNOWN, "test double: unknown focus")


class FlipOnRefreshPresence(_BasePresence):
    """Absent until refresh() runs, then flips to the configured state."""

    def __init__(self, state: PresenceState = PresenceState.PRESENT) -> None:
        super().__init__(PresenceState.ABSENT, "test double: absent so far")
        self._flip_state = state

    async def refresh(self) -> None:
        await super().refresh()
        if self.refreshes == 1:
            self.set_state(self._flip_state, "test double: focus arrived")

    async def wait_for_change(self, after_revision: int) -> int:
        while self._revision <= after_revision:
            await asyncio.sleep(0)
        return self._revision
