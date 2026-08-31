"""Conservative Pi terminal-screen classification."""

from __future__ import annotations

from theater.harness.contracts.callbacks import ScreenContext
from theater.harness.observation import ScreenConfidence, ScreenKind, ScreenReading
from theater.harness.transcript.discovery import screen_tail


def classify_screen(context: ScreenContext) -> ScreenReading:
    lines = [line.strip().lower() for line in screen_tail(context.capture, 8) if line.strip()]
    if any("esc" in line and "interrupt" in line for line in lines):
        return ScreenReading(kind=ScreenKind.WORKING, confidence=ScreenConfidence.HIGH)
    if lines and lines[-1] in {">", "›", "❯"}:
        return ScreenReading(kind=ScreenKind.PROMPT, confidence=ScreenConfidence.MEDIUM)
    return ScreenReading(kind=ScreenKind.UNKNOWN, confidence=ScreenConfidence.LOW)
