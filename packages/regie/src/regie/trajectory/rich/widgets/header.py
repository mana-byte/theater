"""One-line trajectory status and totals."""

from __future__ import annotations

from textual.widgets import Static

from regie.trajectory.domain import PanelStateInfo, TrajectoryOverview
from regie.trajectory.rich.render.summary import summary_line
from regie.trajectory.ui_constants import (
    TRAJECTORY_HEADER_HEIGHT,
    TRAJECTORY_OVERVIEW_TICK_SECONDS,
)


class TrajectoryHeader(Static):
    """Current status, tokens, cost, and active time; ticks while work runs."""

    DEFAULT_CSS = f"""
    TrajectoryHeader {{
        width: 1fr;
        height: {TRAJECTORY_HEADER_HEIGHT};
        min-height: {TRAJECTORY_HEADER_HEIGHT};
        padding: 1 2;
        background: $foreground 4%;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }}
    """

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._inputs: tuple[PanelStateInfo, TrajectoryOverview, bool, str] | None = None

    def on_mount(self) -> None:
        self.set_interval(TRAJECTORY_OVERVIEW_TICK_SECONDS, self._render_line)

    def update_state(
        self,
        *,
        panel: PanelStateInfo,
        overview: TrajectoryOverview,
        loading: bool,
        stale_message: str = "",
    ) -> None:
        inputs = (panel, overview, loading, stale_message)
        if inputs != self._inputs:
            self._inputs = inputs
            self._render_line()

    def _render_line(self) -> None:
        if self._inputs is not None:
            panel, overview, loading, stale_message = self._inputs
            self.update(summary_line(panel, overview, loading=loading, stale_message=stale_message))


__all__ = ["TrajectoryHeader"]
