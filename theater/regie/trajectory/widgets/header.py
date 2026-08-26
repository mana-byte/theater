"""Trajectory context header with an overlaid search drawer."""

from textual.app import ComposeResult
from textual.containers import Vertical

from theater.constants.regie_trajectory import TRAJECTORY_BREADCRUMB_HEIGHT
from theater.regie.trajectory.widgets.breadcrumb import TrajectoryBreadcrumb
from theater.regie.trajectory.widgets.search import TrajectorySearchInput


class TrajectoryHeader(Vertical):
    """Show selection context until search slides over it."""

    DEFAULT_CSS = f"""
    TrajectoryHeader {{
        width: 1fr;
        min-width: 0;
        height: {TRAJECTORY_BREADCRUMB_HEIGHT};
        min-height: {TRAJECTORY_BREADCRUMB_HEIGHT};
        layers: trajectory-header-content trajectory-header-search;
        overflow-x: hidden;
        overflow-y: hidden;
    }}
    TrajectoryHeader > TrajectoryBreadcrumb {{
        layer: trajectory-header-content;
    }}
    TrajectoryHeader > TrajectorySearchInput {{
        dock: top;
        layer: trajectory-header-search;
    }}
    """

    def compose(self) -> ComposeResult:
        yield TrajectoryBreadcrumb(id="trajectory-breadcrumb")
        yield TrajectorySearchInput(
            placeholder="⌕ Search trajectory  /",
            id="trajectory-search",
        )


__all__ = ["TrajectoryHeader"]
