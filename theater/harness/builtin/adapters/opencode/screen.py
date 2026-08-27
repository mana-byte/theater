"""OpenCode screen classification."""

from __future__ import annotations

from theater.harness.observation import ScreenConfidence, ScreenKind, ScreenReading

from .constants import (
    _SCREEN_TAIL_LINES,
    APPROVAL_MARKER,
    FOOTER_MARKER,
    QUESTION_MARKER,
    WORKING_MARKERS,
)


def _in_screen_tail(capture: str, marker: str) -> bool:
    """Match footer chrome only, not similarly worded agent output."""
    lines = [line for line in capture.splitlines() if line.strip()]
    return any(marker in line and FOOTER_MARKER in line for line in lines[-_SCREEN_TAIL_LINES:])


def is_idle_screen(capture: str) -> bool:
    if any(_in_screen_tail(capture, marker) for marker in WORKING_MARKERS):
        return False
    return FOOTER_MARKER in capture


def screen_reading(capture: str) -> ScreenReading:
    if any(_in_screen_tail(capture, marker) for marker in WORKING_MARKERS):
        return ScreenReading(kind=ScreenKind.WORKING, confidence=ScreenConfidence.HIGH)
    prompt_chrome = FOOTER_MARKER in capture or any(marker in capture for marker in WORKING_MARKERS)
    if not prompt_chrome and (APPROVAL_MARKER in capture or QUESTION_MARKER in capture):
        return ScreenReading(kind=ScreenKind.APPROVAL, confidence=ScreenConfidence.HIGH)
    if is_idle_screen(capture):
        return ScreenReading(kind=ScreenKind.PROMPT, confidence=ScreenConfidence.HIGH)
    return ScreenReading(kind=ScreenKind.UNKNOWN, confidence=ScreenConfidence.LOW)
