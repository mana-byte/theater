"""Claude tmux-screen classification."""

from __future__ import annotations

import re

from theater.harness.base import last_screen_line
from theater.harness.observation import ScreenConfidence, ScreenKind, ScreenReading
from theater.harness.transcript.discovery import screen_tail

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

# Permission-mode footer family, from the claude-code 2.1.220 bundle (external
# evidence): each symbol renders with only its own indicators, so impossible
# pairings (⏸ bypass permissions) stay out of the family.
_MODE_LINE_PAIRS = (
    ("⏸", ("plan mode", "manual mode")),
    ("⏵⏵", ("accept edits", "bypass permissions", "don't ask", "auto mode")),
)
#: Composed at import: the footer is symbol + indicator + optional " on"
#: + optional parenthesized hint, so the regex is built from the pairs.
_MODE_LINE_RE = re.compile(
    r"^(?:"
    + "|".join(
        rf"{re.escape(symbol)} (?:"
        + "|".join(re.escape(indicator) for indicator in indicators)
        + r")"
        for symbol, indicators in _MODE_LINE_PAIRS
    )
    + r")(?: on)?(?: \([^()]*\))?$"
)


def _is_mode_line_footer(line: str) -> bool:
    return _MODE_LINE_RE.match(line.strip()) is not None


class ClaudeScreen:
    def is_idle_screen(self, capture: str) -> bool:
        return self.screen_reading(capture).kind is ScreenKind.PROMPT

    def screen_reading(self, capture: str) -> ScreenReading:
        """Classify display hints in safety order: trust, approval, working, prompt.

        Trust and approval precede prompt because injecting Enter there changes permissions.
        Working precedes prompt so a mixed frame cannot finish a live turn early.
        """
        if TRUST_MARKER in capture:
            return ScreenReading(kind=ScreenKind.TRUST, confidence=ScreenConfidence.HIGH)
        if APPROVAL_MARKER in capture:
            return ScreenReading(kind=ScreenKind.APPROVAL, confidence=ScreenConfidence.HIGH)
        tail = screen_tail(capture, _SCREEN_TAIL_LINES)
        if any(WORKING_MARKER in line for line in tail):
            return ScreenReading(kind=ScreenKind.WORKING, confidence=ScreenConfidence.HIGH)
        if last_screen_line(capture) in IDLE_PROMPTS:
            return ScreenReading(kind=ScreenKind.PROMPT, confidence=ScreenConfidence.HIGH)
        if any(IDLE_FOOTER in line for line in tail):
            return ScreenReading(kind=ScreenKind.PROMPT, confidence=ScreenConfidence.HIGH)
        if any(
            line.strip().startswith(MODE_LINE_PREFIXES)
            and line.rstrip().endswith(IDLE_AGENTS_FOOTER)
            for line in tail
        ):
            return ScreenReading(kind=ScreenKind.PROMPT, confidence=ScreenConfidence.HIGH)
        # Dottore–Giacinto debate, accepted residual: without the working marker
        # a bottommost mode footer reads PROMPT — the redraw flicker trades for
        # the stranded-job bug it fixes.
        if _is_mode_line_footer(last_screen_line(capture)):
            return ScreenReading(kind=ScreenKind.PROMPT, confidence=ScreenConfidence.HIGH)
        return ScreenReading(kind=ScreenKind.UNKNOWN, confidence=ScreenConfidence.LOW)
