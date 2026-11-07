"""Claude tmux-screen classification."""

from __future__ import annotations

import re
from collections.abc import Sequence

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
    NARROW_PANE_COLUMNS,
    TRUST_MARKER,
    WORKING_MARKER,
)

# Permission-mode footer family (external evidence): each symbol renders with
# only its own indicators, so impossible pairings (⏸ bypass permissions) stay
# out of the family.
_MODE_LINE_PAIRS = (
    ("⏸", ("plan mode", "manual mode")),
    ("⏵⏵", ("accept edits", "bypass permissions", "don't ask", "auto mode")),
)
#: Joined symbol + indicator alternatives, shared by both footer shapes.
_MODE_LINE_SYMBOLS = "|".join(
    rf"{re.escape(symbol)} (?:" + "|".join(re.escape(indicator) for indicator in indicators) + r")"
    for symbol, indicators in _MODE_LINE_PAIRS
)
#: The plain footer shape: symbol + indicator + optional " on" + optional hint.
_MODE_LINE_PLAIN = r"(?:" + _MODE_LINE_SYMBOLS + r")(?: on)?(?: \([^()]*\))?"
#: Composed at import: the plain footer, or the agent-count footer with its fixed tail.
_MODE_LINE_RE = re.compile(
    r"^(?:"
    + _MODE_LINE_PLAIN
    + r"|(?:"
    + _MODE_LINE_SYMBOLS
    + r") on \(shift\+tab to cycle\) · ← \d+ agents?"
    + r"(?:[ \t]+new task\? /clear to save \d+(?:\.\d+)?k tokens)?)$"
)
_PLAIN_MODE_LINE_RE = re.compile(r"^" + _MODE_LINE_PLAIN + r"$")


def _is_mode_line_footer(line: str) -> bool:
    return _MODE_LINE_RE.match(line.strip()) is not None


def _narrow_composer(tail: Sequence[str]) -> bool:
    """True when the tail's composer separator lines are all narrower than the pane bound."""
    separators = [line.strip() for line in tail if line.strip() and set(line.strip()) == {"─"}]
    return bool(separators) and all(len(line) < NARROW_PANE_COLUMNS for line in separators)


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
        # the stranded-job bug it fixes. In a narrow pane the bare footer is
        # identical to the idle one, so there it proves nothing.
        last_line = last_screen_line(capture)
        if _is_mode_line_footer(last_line):
            if _PLAIN_MODE_LINE_RE.match(last_line.strip()) and _narrow_composer(tail):
                return ScreenReading(kind=ScreenKind.UNKNOWN, confidence=ScreenConfidence.LOW)
            return ScreenReading(kind=ScreenKind.PROMPT, confidence=ScreenConfidence.HIGH)
        return ScreenReading(kind=ScreenKind.UNKNOWN, confidence=ScreenConfidence.LOW)
