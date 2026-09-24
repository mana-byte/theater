"""Contextual key hints and live status for the trajectory view."""

from __future__ import annotations

from rich.text import Text
from textual.widget import Widget

from regie.trajectory.ui_constants import TRAJECTORY_FOOTER_HEIGHT

_TIMELINE_KEYS = (
    ("j k", "lane"),
    ("h l", "span in lane"),
    ("⏎", "details"),
    ("/", "search"),
    ("n N", "match"),
    ("+ -", "zoom"),
    ("H L", "first/live"),
    ("esc", "close"),
)
_DETAIL_KEYS = (("j k", "scroll"), ("h l", "tab"), ("y", "copy"), ("esc", "timeline"))


class TrajectoryFooter(Widget):
    """Key hints for the focused region, then follow and search status."""

    DEFAULT_CSS = f"""
    TrajectoryFooter {{
        width: 1fr;
        height: {TRAJECTORY_FOOTER_HEIGHT};
        min-height: {TRAJECTORY_FOOTER_HEIGHT};
        padding: 0 2;
        background: $foreground 4%;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }}
    """

    _key: tuple[bool, str] | None = None

    def update_state(self, *, detail_focused: bool, status: str) -> None:
        if (detail_focused, status) != self._key:
            self._key = (detail_focused, status)
            self.refresh()

    def render(self) -> Text:
        detail_focused, status = self._key or (False, "")
        line = Text(no_wrap=True, overflow="ellipsis")
        for key, label in _DETAIL_KEYS if detail_focused else _TIMELINE_KEYS:
            line.append(f" {key} ", style="bold reverse")
            line.append(f" {label}   ", style="dim")
        if status:
            line.append(f"│  {status}", style="italic")
        return line


__all__ = ["TrajectoryFooter"]
