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
    """Whether some tail line contains every marker in `markers`.

    The spinner and footer chrome always render at the bottom of the pane, and
    matching the whole pane lets agent output impersonate chrome — e.g. the
    phrase ``to interrupt`` is ordinary English that an agent can echo. Scoping
    to the tail is necessary but not sufficient: the tail also contains the
    agent's closing lines. Requiring a second token (``Esc``) on the same line
    is what distinguishes the spinner from prose.
    """
    lines = capture.splitlines()
    return any(all(m in line for m in markers) for line in lines[-limit:] if line)


class VibeScreenMixin:
    def is_idle_screen(self, capture: str) -> bool:
        """Vibe shows a bare `❯` prompt when waiting for input.

        The capture-pane output ends with the current input line. If the
        last non-empty line is just the prompt symbol (with optional
        whitespace), the agent is idle. If there is text after the prompt,
        someone is typing — but that's human presence, not idle. If the
        last line is agent output, the agent is still rendering.
        """
        return last_screen_line(capture) in IDLE_PROMPTS

    def screen_reading(self, capture: str) -> ScreenReading:
        """Classify the screen as `trust`, `approval`, `working`, `prompt` or `unknown`.

        Order is load-bearing here, twice over, because vibe keeps drawing the
        composer and the spinner underneath its permission box:

        Trust before everything: the trust dialog runs at startup, before any
        turn or prompt, and is a modal that blocks all interaction — including
        the send gate. It must win over any other marker.

        Approval before working, because `WORKING_MARKER` is on screen in the
        same capture as the permission box — check working first and every
        dialog reads as `working`, so AWAITING_INPUT is never reachable.

        Working before prompt, because the composer's empty prompt line stays
        on screen during a turn. Reading a working screen as a prompt does not
        merely mislabel it: the reducer maps `prompt` to IDLE, and
        `_rescue_jobs` then finishes the agent's jobs mid-turn, resolving the
        caller's `await` on a turn that never ended.

        The prompt is found by scanning the tail rather than checking only the
        last line: a real capture has a separator and a cwd/token footer below
        it, so `is_idle_screen` does not fire on a real screen.
        """
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
