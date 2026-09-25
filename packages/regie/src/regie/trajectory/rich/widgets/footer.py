"""Contextual key hints and live status for the trajectory view."""

from __future__ import annotations

from rich.style import Style
from rich.text import Text
from textual import events
from textual.message import Message
from textual.widget import Widget

from regie.trajectory.ui_constants import TRAJECTORY_FOOTER_HEIGHT

_TIMELINE_KEYS = (
    ("j k", "lane"),
    ("h l", "span in lane"),
    ("⏎ J", "details"),
    ("/", "search"),
    ("n N", "match"),
    ("f", "filter"),
    ("E", "export"),
    ("+ -", "zoom"),
    ("H L", "first/live"),
    ("esc", "close"),
)
_DETAIL_KEYS = (
    ("j k", "move"),
    ("h l", "scroll"),
    ("⏎", "fold"),
    ("y", "copy section"),
    ("Y", "copy all"),
    ("f", "filter"),
    ("E", "export"),
    ("K esc", "timeline"),
)


FOOTER_KEY_META = "trajectory_footer_key"
# Hint glyphs spelled as the key names the view's bindings use.
_KEY_NAMES = {"⏎": "enter", "esc": "escape", "+": "plus", "-": "minus"}
_KEY_STYLE = Style(bold=True, reverse=True)


class FooterKeyClicked(Message):
    """A key hint was clicked; the view runs it as if the key were pressed."""

    def __init__(self, key: str) -> None:
        super().__init__()
        self.key = key


class TrajectoryFooter(Widget):
    """Clickable key hints for the focused region, then follow and search status."""

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
        for keys, label in _DETAIL_KEYS if detail_focused else _TIMELINE_KEYS:
            tokens = keys.split()
            line.append(" ", style=_KEY_STYLE)
            for index, token in enumerate(tokens):
                click = Style(meta={FOOTER_KEY_META: _KEY_NAMES.get(token, token)})
                line.append((" " if index else "") + token, style=_KEY_STYLE + click)
            line.append(" ", style=_KEY_STYLE)
            first = Style(meta={FOOTER_KEY_META: _KEY_NAMES.get(tokens[0], tokens[0])})
            line.append(f" {label}", style=Style(dim=True) + first)
            line.append("   ")
        if status:
            line.append(f"│  {status}", style="italic")
        return line

    def on_click(self, event: events.Click) -> None:
        key = event.style.meta.get(FOOTER_KEY_META)
        if isinstance(key, str):
            event.stop()
            self.post_message(FooterKeyClicked(key))


__all__ = ["FOOTER_KEY_META", "FooterKeyClicked", "TrajectoryFooter"]
