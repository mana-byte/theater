"""Bounded, process-local trajectory navigation history."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from theater.constants.regie_trajectory import TRAJECTORY_NAVIGATION_HISTORY_LIMIT
from theater.constants.trajectory import TRAJECTORY_IDENTIFIER_MAX_BYTES


@dataclass(frozen=True, slots=True)
class TrajectoryNavigationTarget:
    participant_id: str
    record_id: str

    def __post_init__(self) -> None:
        for name in ("participant_id", "record_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"navigation {name} must be a non-empty string")
            if len(value.encode("utf-8")) > TRAJECTORY_IDENTIFIER_MAX_BYTES:
                raise ValueError(f"navigation {name} is too large")


class TrajectoryNavigationHistory:
    def __init__(self, limit: int = TRAJECTORY_NAVIGATION_HISTORY_LIMIT) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("navigation history limit must be a positive integer")
        self._entries: deque[TrajectoryNavigationTarget] = deque(maxlen=limit)

    @property
    def entries(self) -> tuple[TrajectoryNavigationTarget, ...]:
        return tuple(self._entries)

    def push(self, participant_id: str, record_id: str) -> bool:
        target = TrajectoryNavigationTarget(participant_id, record_id)
        if self._entries and self._entries[-1] == target:
            return False
        self._entries.append(target)
        return True

    def back(self) -> TrajectoryNavigationTarget | None:
        return self._entries.pop() if self._entries else None

    def clear(self) -> None:
        self._entries.clear()


__all__ = ["TrajectoryNavigationHistory", "TrajectoryNavigationTarget"]
