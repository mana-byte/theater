"""Right-hand Régie surface state, independent of local terminal mutation."""

from __future__ import annotations

from enum import StrEnum


class SurfaceMode(StrEnum):
    DASHBOARD = "dashboard"
    TRAJECTORY = "trajectory"


class SurfaceController:
    """Keep one visible public surface and its stable participant selection."""

    def __init__(self) -> None:
        self._mode = SurfaceMode.DASHBOARD
        self._trajectory_participant_id: str | None = None

    @property
    def mode(self) -> SurfaceMode:
        return self._mode

    @property
    def trajectory_participant_id(self) -> str | None:
        return self._trajectory_participant_id

    def show_dashboard(self) -> None:
        self._mode = SurfaceMode.DASHBOARD
        self._trajectory_participant_id = None

    def show_trajectory(self, participant_id: str) -> None:
        self._mode = SurfaceMode.TRAJECTORY
        self._trajectory_participant_id = participant_id


__all__ = ["SurfaceController", "SurfaceMode"]
