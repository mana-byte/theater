"""Codex tmux-screen classification."""

from __future__ import annotations

import re

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


def _in_screen_tail(capture: str, marker: str) -> bool:
    """Match footer markers only in the recent screen tail."""
    lines = [line.strip() for line in screen_tail(capture, _SCREEN_TAIL_LINES)]
    return any(line.endswith(marker) for line in lines)


def _shows_trust_dialog(capture: str) -> bool:
    """A trust header counts only with dialog chrome in the bounded tail.

    Chrome — a numbered option row or a press-enter line — separates a live
    dialog from assistant prose that merely quotes a trust header.
    """
    lines = [line.strip() for line in screen_tail(capture, _MODAL_TAIL_LINES)]
    header = any(marker in line for line in lines for marker in TRUST_MARKERS)
    chrome = any(_MENU_OPTION.match(line) or "press enter" in line.lower() for line in lines)
    return header and chrome


class CodexScreenMixin:
    def is_idle_screen(self, capture: str) -> bool:
        if WORKING_MARKER in capture:
            return False
        lines = [line.strip() for line in screen_tail(capture, _SCREEN_TAIL_LINES)]
        return any(line.startswith(PROMPT) and _MENU_OPTION.match(line) is None for line in lines)

    def screen_reading(self, capture: str) -> ScreenReading:
        """Classify modal states before prompt rows."""
        if _shows_trust_dialog(capture):
            return ScreenReading(kind=ScreenKind.TRUST, confidence=ScreenConfidence.HIGH)
        if _in_screen_tail(capture, APPROVAL_MARKER):
            return ScreenReading(kind=ScreenKind.APPROVAL, confidence=ScreenConfidence.HIGH)
        if WORKING_MARKER in capture:
            return ScreenReading(kind=ScreenKind.WORKING, confidence=ScreenConfidence.HIGH)
        if self.is_idle_screen(capture):
            return ScreenReading(kind=ScreenKind.PROMPT, confidence=ScreenConfidence.HIGH)
        return ScreenReading(kind=ScreenKind.UNKNOWN, confidence=ScreenConfidence.LOW)


def screen_reading(context: ScreenContext) -> ScreenReading:
    return CodexScreenMixin().screen_reading(context.capture)
