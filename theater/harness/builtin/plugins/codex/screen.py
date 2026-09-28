"""Codex tmux-screen classification."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from theater.harness.contracts.callbacks import ScreenContext
from theater.harness.observation import ScreenConfidence, ScreenKind, ScreenReading
from theater.harness.transcript.discovery import screen_tail

from .constants import (
    _MODAL_TAIL_LINES,
    _SCREEN_TAIL_LINES,
    APPROVAL_MARKER,
    PROMPT,
    TRUST_MARKERS,
    WORKING_MARKER,
)

#: A numbered menu row (e.g. `› 1. Trust and continue`) is a dialog option,
#: never the composer.
_MENU_OPTION = re.compile(r"^›?\s*\d+\.\s")


def _is_composer(line: str) -> bool:
    return line.startswith(PROMPT) and _MENU_OPTION.match(line) is None


def _lowest(lines: Sequence[str], predicate: Callable[[str], bool]) -> int | None:
    """Index of the bottom-most line matching ``predicate``, else None."""
    for index in range(len(lines) - 1, -1, -1):
        if predicate(lines[index]):
            return index
    return None


def _trust_bottom(lines: Sequence[str]) -> int | None:
    """Bottom line of trust evidence: a header plus dialog chrome."""
    header = _lowest(lines, lambda line: any(marker in line for marker in TRUST_MARKERS))
    chrome = _lowest(
        lines,
        lambda line: _MENU_OPTION.match(line) is not None or "press enter" in line.lower(),
    )
    if header is None or chrome is None:
        return None
    return max(header, chrome)


def _live_modal(capture: str) -> ScreenKind | None:
    """Only a later composer dismisses quoted modal evidence."""
    lines = [line.strip() for line in screen_tail(capture, _MODAL_TAIL_LINES)]
    composer = _lowest(lines, _is_composer)
    trust = _trust_bottom(lines)
    if trust is not None and (composer is None or composer < trust):
        return ScreenKind.TRUST
    approval = _lowest(lines, lambda line: line.endswith(APPROVAL_MARKER))
    if approval is not None and (composer is None or composer < approval):
        return ScreenKind.APPROVAL
    return None


class CodexScreenMixin:
    def is_idle_screen(self, capture: str) -> bool:
        if WORKING_MARKER in capture or _live_modal(capture) is not None:
            return False
        lines = [line.strip() for line in screen_tail(capture, _SCREEN_TAIL_LINES)]
        composer = _lowest(lines, _is_composer)
        menu = _lowest(lines, lambda line: _MENU_OPTION.match(line) is not None)
        return composer is not None and (menu is None or composer > menu)

    def screen_reading(self, capture: str) -> ScreenReading:
        """Classify live modals before working and prompt rows."""
        modal = _live_modal(capture)
        if modal is not None:
            return ScreenReading(kind=modal, confidence=ScreenConfidence.HIGH)
        if WORKING_MARKER in capture:
            return ScreenReading(kind=ScreenKind.WORKING, confidence=ScreenConfidence.HIGH)
        if self.is_idle_screen(capture):
            return ScreenReading(kind=ScreenKind.PROMPT, confidence=ScreenConfidence.HIGH)
        return ScreenReading(kind=ScreenKind.UNKNOWN, confidence=ScreenConfidence.LOW)


def screen_reading(context: ScreenContext) -> ScreenReading:
    return CodexScreenMixin().screen_reading(context.capture)
