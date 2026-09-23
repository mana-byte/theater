"""Vibe screen classification."""

from __future__ import annotations

import re

from theater.harness.base import last_screen_line
from theater.harness.observation import ScreenConfidence, ScreenKind, ScreenReading
from theater.harness.transcript.discovery import screen_tail

from .constants import (
    _SCREEN_IDLE_PROMPTS,
    _SCREEN_TAIL_LINES,
    _SPINNER_TAIL_LINES,
    APPROVAL_MARKER,
    IDLE_PROMPTS,
    QUESTION_FOOTER_MARKERS,
    QUESTION_MARKER,
    TRUST_MARKER,
    WORKING_MARKER,
    WORKING_MARKER_KEY,
)

_QUESTION_STATUS_LINE = re.compile(
    rf"^(?:[\u2800-\u28ff]+|[■□])\s+{re.escape(QUESTION_MARKER)}(?:\s|$)"
)


def _in_screen_tail(capture: str, markers: tuple[str, ...], limit: int) -> bool:
    """Match tail chrome without treating agent output as a spinner."""
    lines = screen_tail(capture, limit, skip_blank=False)
    return any(all(m in line for m in markers) for line in lines if line)


def _has_question_dialog(capture: str) -> bool:
    """Pair the action-required loading row with current question chrome."""
    if not _in_screen_tail(capture, QUESTION_FOOTER_MARKERS, _SCREEN_TAIL_LINES):
        return False
    if any(
        line.strip() in _SCREEN_IDLE_PROMPTS for line in screen_tail(capture, _SCREEN_TAIL_LINES)
    ):
        return False
    return any(_QUESTION_STATUS_LINE.match(line.lstrip()) for line in capture.splitlines())


class VibeScreenMixin:
    def is_idle_screen(self, capture: str) -> bool:
        """Recognize Vibe's bare idle prompt."""
        return last_screen_line(capture) in IDLE_PROMPTS

    def screen_reading(self, capture: str) -> ScreenReading:
        """Classify trust, approval, working, prompt, or unknown in precedence order."""
        if TRUST_MARKER in capture:
            return ScreenReading(kind=ScreenKind.TRUST, confidence=ScreenConfidence.HIGH)
        if APPROVAL_MARKER in capture or _has_question_dialog(capture):
            return ScreenReading(kind=ScreenKind.APPROVAL, confidence=ScreenConfidence.HIGH)
        if _in_screen_tail(capture, (WORKING_MARKER, WORKING_MARKER_KEY), _SPINNER_TAIL_LINES):
            return ScreenReading(kind=ScreenKind.WORKING, confidence=ScreenConfidence.HIGH)
        lines = [line.strip() for line in screen_tail(capture, _SCREEN_TAIL_LINES)]
        if any(line in _SCREEN_IDLE_PROMPTS for line in lines):
            return ScreenReading(kind=ScreenKind.PROMPT, confidence=ScreenConfidence.HIGH)
        return ScreenReading(kind=ScreenKind.UNKNOWN, confidence=ScreenConfidence.LOW)
