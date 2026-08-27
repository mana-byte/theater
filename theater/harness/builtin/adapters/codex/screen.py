from __future__ import annotations

from theater.harness.observation import ScreenConfidence, ScreenKind, ScreenReading

from .constants import (
    _SCREEN_TAIL_LINES,
    APPROVAL_MARKER,
    PROMPT,
    TRUST_MARKER,
    WORKING_MARKER,
)


def _in_screen_tail(capture: str, marker: str) -> bool:
    """Match footer markers only in the recent screen tail."""
    lines = [line.strip() for line in capture.splitlines() if line.strip()]
    return any(line.endswith(marker) for line in lines[-_SCREEN_TAIL_LINES:])


class CodexScreenMixin:
    def is_idle_screen(self, capture: str) -> bool:
        if WORKING_MARKER in capture:
            return False
        lines = [line.strip() for line in capture.splitlines() if line.strip()]
        return any(line.startswith(PROMPT) for line in lines[-_SCREEN_TAIL_LINES:])

    def screen_reading(self, capture: str) -> ScreenReading:
        """Classify modal states before prompt rows."""
        if TRUST_MARKER in capture:
            return ScreenReading(kind=ScreenKind.TRUST, confidence=ScreenConfidence.HIGH)
        if _in_screen_tail(capture, APPROVAL_MARKER):
            return ScreenReading(kind=ScreenKind.APPROVAL, confidence=ScreenConfidence.HIGH)
        if WORKING_MARKER in capture:
            return ScreenReading(kind=ScreenKind.WORKING, confidence=ScreenConfidence.HIGH)
        if self.is_idle_screen(capture):
            return ScreenReading(kind=ScreenKind.PROMPT, confidence=ScreenConfidence.HIGH)
        return ScreenReading(kind=ScreenKind.UNKNOWN, confidence=ScreenConfidence.LOW)
