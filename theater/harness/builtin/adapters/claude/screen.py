"""Claude tmux-screen classification."""

from __future__ import annotations

from theater.harness.base import last_screen_line
from theater.harness.observation import ScreenConfidence, ScreenKind, ScreenReading

from .constants import (
    _SCREEN_TAIL_LINES,
    APPROVAL_MARKER,
    IDLE_AGENTS_FOOTER,
    IDLE_FOOTER,
    IDLE_PROMPTS,
    MODE_LINE_PREFIXES,
    TRUST_MARKER,
    WORKING_MARKER,
)


class ClaudeScreen:
    def is_idle_screen(self, capture: str) -> bool:
        return last_screen_line(capture) in IDLE_PROMPTS

    def screen_reading(self, capture: str) -> ScreenReading:
        """Classify display hints in safety order: trust, approval, working, prompt."""
        if TRUST_MARKER in capture:
            return ScreenReading(kind=ScreenKind.TRUST, confidence=ScreenConfidence.HIGH)
        if APPROVAL_MARKER in capture:
            return ScreenReading(kind=ScreenKind.APPROVAL, confidence=ScreenConfidence.HIGH)
        lines = [line for line in capture.splitlines() if line.strip()]
        tail = lines[-_SCREEN_TAIL_LINES:]
        if any(WORKING_MARKER in line for line in tail):
            return ScreenReading(kind=ScreenKind.WORKING, confidence=ScreenConfidence.HIGH)
        if any(IDLE_FOOTER in line for line in tail):
            return ScreenReading(kind=ScreenKind.PROMPT, confidence=ScreenConfidence.HIGH)
        if any(
            line.strip().startswith(MODE_LINE_PREFIXES)
            and line.rstrip().endswith(IDLE_AGENTS_FOOTER)
            for line in tail
        ):
            return ScreenReading(kind=ScreenKind.PROMPT, confidence=ScreenConfidence.HIGH)
        return ScreenReading(kind=ScreenKind.UNKNOWN, confidence=ScreenConfidence.LOW)
