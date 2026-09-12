"""Conservative Pi terminal-screen classification."""

from __future__ import annotations

from theater.harness.contracts.callbacks import ScreenContext
from theater.harness.observation import ScreenConfidence, ScreenKind, ScreenReading

# Pi's built-in loader uses these frames in v0.83.0.  Unlike the static
# ``escape interrupt`` help text, a rendered frame is evidence of an active
# status indicator.  The terminal capture is already bounded to the visible
# tmux pane, so search all of it: extension widgets can place the loader well
# above the bottom few lines.
_SPINNER_FRAMES = frozenset("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")

# The bundled Theater extension renders this as its own final footer-status
# line only when Pi reports that it is fully settled.  Keep this exact and
# position-sensitive: assistant prose must never be able to spoof an idle
# screen reading.
_IDLE_MARKER = "theater: idle"

# The bundled Theater extension also renders this footer-status line while
# a user-input tool call (a question, an approval request) is pending
# mid-turn.  It is checked before the spinner: a pane parked on a question
# is still, so no spinner frame competes with it, and Enter is a button
# press there — the same unrecoverable cost as an approval dialog.  Keep
# this exact and position-sensitive like the idle marker: assistant prose
# must never be able to spoof an awaiting reading.
_AWAITING_MARKER = "theater: awaiting input"

# Pi renders every interactive overlay — its own model and thinking
# selectors, permission prompts, and the question tools' dialogs — with this
# cancel affordance in the final chrome line, and an open overlay fully
# covers the status footer.  Claude Code uses the same string as its
# approval marker.  Checked on the final line only, so prose that merely
# mentions the affordance cannot spoof an awaiting reading while chrome is
# present.
_AWAITING_HINT = "esc to cancel"


def _screen_lines(capture: str) -> list[str]:
    return [line.strip().lower() for line in capture.splitlines() if line.strip()]


def _is_spinner_status(line: str) -> bool:
    return len(line) > 2 and line[0] in _SPINNER_FRAMES and line[1].isspace()


def classify_screen(context: ScreenContext) -> ScreenReading:
    lines = _screen_lines(context.capture)
    if lines and lines[-1] == _AWAITING_MARKER:
        return ScreenReading(kind=ScreenKind.APPROVAL, confidence=ScreenConfidence.HIGH)
    if lines and _AWAITING_HINT in lines[-1]:
        return ScreenReading(kind=ScreenKind.APPROVAL, confidence=ScreenConfidence.HIGH)
    if any(_is_spinner_status(line) for line in lines):
        return ScreenReading(kind=ScreenKind.WORKING, confidence=ScreenConfidence.HIGH)
    if lines and lines[-1] == _IDLE_MARKER:
        return ScreenReading(kind=ScreenKind.PROMPT, confidence=ScreenConfidence.HIGH)
    if lines and lines[-1] in {">", "›", "❯"}:
        return ScreenReading(kind=ScreenKind.PROMPT, confidence=ScreenConfidence.LOW)
    return ScreenReading(kind=ScreenKind.UNKNOWN, confidence=ScreenConfidence.LOW)
