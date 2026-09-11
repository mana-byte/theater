"""Shared cached human-presence interface for daemon consumers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class PresenceState(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PresenceSnapshot:
    state: PresenceState
    reason: str
    revision: int
    observed_at: float | None

    @property
    def protected(self) -> bool:
        return self.state is not PresenceState.ABSENT

    def to_dict(self) -> dict:
        return {
            "state": str(self.state),
            "protected": self.protected,
            "reason": self.reason,
            "revision": self.revision,
            "observed_at": self.observed_at,
        }


class PresenceProvider(Protocol):
    @property
    def revision(self) -> int: ...

    def snapshot(self, participant_id: str) -> PresenceSnapshot: ...

    async def refresh(self) -> None: ...

    async def require_absent(self, participant_id: str) -> None: ...

    async def wait_for_change(self, after_revision: int) -> int: ...
