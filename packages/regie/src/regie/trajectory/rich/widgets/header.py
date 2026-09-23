"""One-line trajectory status and totals."""

from __future__ import annotations

from rich.text import Text
from textual.widget import Widget

from regie.trajectory.domain import PanelStateInfo, TrajectoryOverview
from regie.trajectory.rich.render.summary import summary_line
from regie.trajectory.ui_constants import (
    TRAJECTORY_HEADER_HEIGHT,
    TRAJECTORY_OVERVIEW_TICK_SECONDS,
)


class TrajectoryHeader(Widget):
    """Current status, tokens, cost, and active time; repaints without relayout."""

    DEFAULT_CSS = f"""
    TrajectoryHeader {{
        width: 1fr;
        height: {TRAJECTORY_HEADER_HEIGHT};
        min-height: {TRAJECTORY_HEADER_HEIGHT};
        padding: 1 2;
        background: $foreground 4%;
    }}
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._inputs: tuple[PanelStateInfo, TrajectoryOverview, bool, str] | None = None

    def on_mount(self) -> None:
        # Running durations tick; a plain repaint keeps this off the layout path.
        self.set_interval(TRAJECTORY_OVERVIEW_TICK_SECONDS, self.refresh)

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
            self.refresh()

    def render(self) -> Text:
        if self._inputs is None:
            return Text()
        panel, overview, loading, stale_message = self._inputs
        return summary_line(panel, overview, loading=loading, stale_message=stale_message)


__all__ = ["TrajectoryHeader"]
