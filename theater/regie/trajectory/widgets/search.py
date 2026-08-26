"""Animated trajectory search drawer."""

from textual.geometry import Offset
from textual.widgets import Input

from theater.constants.regie_trajectory import (
    SEARCH_HEIGHT,
    TRAJECTORY_SEARCH_SLIDE_EASING,
    TRAJECTORY_SEARCH_SLIDE_SECONDS,
)


class TrajectorySearchInput(Input):
    """Keep search mounted while sliding it over trajectory context."""

    DEFAULT_CSS = f"""
    TrajectorySearchInput {{
        width: 1fr;
        min-width: 0;
        max-width: 100%;
        height: {SEARCH_HEIGHT};
        min-height: {SEARCH_HEIGHT};
        offset-y: -{SEARCH_HEIGHT};
        visibility: hidden;
        padding: 0 1;
        border: solid $foreground 12%;
        background: $foreground 3%;
    }}
    TrajectorySearchInput.-open {{
        visibility: visible;
    }}
    TrajectorySearchInput:focus {{
        border: solid $accent 30%;
        background: $accent 10%;
    }}
    """

    _target_open = False

    def reveal(self, *, animate: bool = True) -> None:
        if self._target_open and animate:
            return
        self._target_open = True
        self.add_class("-open")
        if not animate:
            self.offset = (0, 0)
            return
        self.animate(
            "offset",
            Offset(0, 0),
            duration=TRAJECTORY_SEARCH_SLIDE_SECONDS,
            easing=TRAJECTORY_SEARCH_SLIDE_EASING,
        )

    def conceal(self, *, animate: bool = True) -> None:
        if not self._target_open:
            if not animate:
                self.offset = (0, -SEARCH_HEIGHT)
                self.remove_class("-open")
            return
        self._target_open = False
        if not animate:
            self.offset = (0, -SEARCH_HEIGHT)
            self.remove_class("-open")
            return
        self.animate(
            "offset",
            Offset(0, -SEARCH_HEIGHT),
            duration=TRAJECTORY_SEARCH_SLIDE_SECONDS,
            easing=TRAJECTORY_SEARCH_SLIDE_EASING,
            on_complete=self._finish_conceal,
        )

    def _finish_conceal(self) -> None:
        if not self._target_open:
            self.remove_class("-open")


__all__ = ["TrajectorySearchInput"]
