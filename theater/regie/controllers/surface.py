"""Right-side dashboard, trajectory, and physical-pane coordination."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class PaneParking(Protocol):
    async def break_pane(self, pane_id: str, *, target_window: str | None = ...) -> None: ...

    async def pane_exists(self, pane_id: str) -> bool: ...


class RightSurface(Enum):
    DASHBOARD = "dashboard"
    TRAJECTORY = "trajectory"


class TrajectoryStageOutcome(Enum):
    STAGED = "staged"
    FOCUS = "focus"
    NO_NODE = "no_node"
    UNMANAGED = "unmanaged"
    PARK_FAILED = "park_failed"
    FOOTER_ACTIVE = "footer_active"


@dataclass(frozen=True, slots=True)
class TrajectoryStageResult:
    outcome: TrajectoryStageOutcome
    staged_pane: str | None
    participant_id: str | None = None
    error: str | None = None


class SurfaceController:
    """Own right-surface selection without importing Textual."""

    def __init__(self, panes: PaneParking) -> None:
        self._panes = panes
        self.surface = RightSurface.DASHBOARD
        self.trajectory_participant: str | None = None

    def trajectory_visible(self, staged_pane: str | None) -> bool:
        return (
            staged_pane is None
            and self.surface is RightSurface.TRAJECTORY
            and self.trajectory_participant is not None
        )

    def show_dashboard(self) -> None:
        """Forget the selected trajectory and restore the dashboard surface."""
        self.surface = RightSurface.DASHBOARD
        self.trajectory_participant = None

    async def stage_trajectory(
        self,
        *,
        participant_id: str | None,
        managed: bool,
        staged_pane: str | None,
        footer_active: bool,
    ) -> TrajectoryStageResult:
        if footer_active:
            return TrajectoryStageResult(TrajectoryStageOutcome.FOOTER_ACTIVE, staged_pane)
        if participant_id is None:
            return TrajectoryStageResult(TrajectoryStageOutcome.NO_NODE, staged_pane)
        if not managed:
            return TrajectoryStageResult(
                TrajectoryStageOutcome.UNMANAGED,
                staged_pane,
                participant_id,
            )
        if self.trajectory_visible(staged_pane) and self.trajectory_participant == participant_id:
            return TrajectoryStageResult(
                TrajectoryStageOutcome.FOCUS,
                staged_pane,
                participant_id,
            )
        if staged_pane is not None:
            try:
                await self._panes.break_pane(staged_pane)
            except Exception as exc:
                try:
                    pane_still_exists = await self._panes.pane_exists(staged_pane)
                except Exception:
                    pane_still_exists = True
                if pane_still_exists:
                    return TrajectoryStageResult(
                        TrajectoryStageOutcome.PARK_FAILED,
                        staged_pane,
                        participant_id,
                        str(exc),
                    )
        self.surface = RightSurface.TRAJECTORY
        self.trajectory_participant = participant_id
        return TrajectoryStageResult(
            TrajectoryStageOutcome.STAGED,
            None,
            participant_id,
        )


__all__ = [
    "RightSurface",
    "SurfaceController",
    "TrajectoryStageOutcome",
    "TrajectoryStageResult",
]
