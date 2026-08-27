"""Vibe screen classification."""

from __future__ import annotations

from theater.harness.base import last_screen_line
from theater.harness.observation import ScreenConfidence, ScreenKind, ScreenReading

from .constants import (
    _SCREEN_IDLE_PROMPTS,
    _SCREEN_TAIL_LINES,
    _SPINNER_TAIL_LINES,
    APPROVAL_MARKER,
    IDLE_PROMPTS,
    TRUST_MARKER,
    WORKING_MARKER,
    WORKING_MARKER_KEY,
)


def _in_screen_tail(capture: str, markers: tuple[str, ...], limit: int) -> bool:
    """Match tail chrome without treating agent output as a spinner."""
    lines = capture.splitlines()
    return any(all(m in line for m in markers) for line in lines[-limit:] if line)


class VibeScreenMixin:
    def is_idle_screen(self, capture: str) -> bool:
        """Recognize Vibe's bare idle prompt."""
        return last_screen_line(capture) in IDLE_PROMPTS

    def screen_reading(self, capture: str) -> ScreenReading:
        """Classify trust, approval, working, prompt, or unknown in precedence order."""
        if TRUST_MARKER in capture:
            return ScreenReading(kind=ScreenKind.TRUST, confidence=ScreenConfidence.HIGH)
        if APPROVAL_MARKER in capture:
            return ScreenReading(kind=ScreenKind.APPROVAL, confidence=ScreenConfidence.HIGH)
        if _in_screen_tail(capture, (WORKING_MARKER, WORKING_MARKER_KEY), _SPINNER_TAIL_LINES):
            return ScreenReading(kind=ScreenKind.WORKING, confidence=ScreenConfidence.HIGH)
        lines = [line.strip() for line in capture.splitlines() if line.strip()]
        if any(line in _SCREEN_IDLE_PROMPTS for line in lines[-_SCREEN_TAIL_LINES:]):
            return ScreenReading(kind=ScreenKind.PROMPT, confidence=ScreenConfidence.HIGH)
        return ScreenReading(kind=ScreenKind.UNKNOWN, confidence=ScreenConfidence.LOW)
