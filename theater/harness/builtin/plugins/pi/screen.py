"""Conservative Pi terminal-screen classification."""

from __future__ import annotations

from theater.harness.contracts.callbacks import ScreenContext
from theater.harness.observation import ScreenConfidence, ScreenKind, ScreenReading

# Pi v0.83.0 loader frames: unlike static help text, a frame proves an active status.
# Search the whole (bounded) capture, since widgets can push the loader up.
_SPINNER_FRAMES = frozenset("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")

# The bundled Theater extension renders this as its own final footer-status
# line only when Pi reports that it is fully settled.  Keep this exact and
# position-sensitive: assistant prose must never be able to spoof an idle
# screen reading.
_IDLE_MARKER = "theater: idle"

# Theater extension footer while a user-input tool is pending; checked before the spinner
# (Enter would press a button). Exact and position-sensitive so prose cannot spoof it.
_AWAITING_MARKER = "theater: awaiting input"

# Every Pi overlay shows this cancel affordance on the final chrome line (Claude uses it too);
# final-line only so prose mentioning it cannot spoof an awaiting reading.
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
