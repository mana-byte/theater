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
    """Whether any of the last few non-blank lines *ends with* *marker*.

    The approval footer is chrome the CLI always draws at the bottom of the
    modal, so searching the whole pane buys nothing — and matching the whole
    pane lets agent output (ordinary prose) impersonate the footer. Scoping
    to the same tail window ``is_idle_screen`` uses is necessary but not
    sufficient on its own: a real codex idle pane has agent output in three
    of the five scanned tail lines (see ``codex_idle.txt``), so the window
    unavoidably contains prose. The end-of-line anchor is the second guard:
    the footer is a whole line that ends with the marker, while prose
    containing the phrase virtually never ends a line with it. Neither the
    tail window nor the endswith test alone is enough; both are required.
    """
    lines = [line.strip() for line in capture.splitlines() if line.strip()]
    return any(line.endswith(marker) for line in lines[-_SCREEN_TAIL_LINES:])


class CodexScreenMixin:
    def is_idle_screen(self, capture: str) -> bool:
        """Codex keeps a status footer below the composer.

        So the bottom line is never the prompt and `last_screen_line` — which
        both other adapters use — would never match. Instead: a running turn
        always renders `esc to interrupt`, and an idle one renders a composer
        line starting with `›` somewhere in the last few lines.

        The composer shows greyed-out placeholder text when empty ("Explain
        this codebase"), and a colourless capture cannot tell that apart from
        a human's half-typed message. That is tolerable because this method
        only feeds the AWAITING_INPUT display hint; whether a human is present
        is decided separately, from `pane_in_mode`, and never from a scrape.

        The first-launch trust dialog also trips this boolean, because it
        renders a `›` selection row just like the idle composer. That is why
        `screen_reading` must check the TRUST and APPROVAL markers before
        falling through to this method: without that guard both modals would
        classify as PROMPT and the send gate would inject into them.
        """
        if WORKING_MARKER in capture:
            return False
        lines = [line.strip() for line in capture.splitlines() if line.strip()]
        return any(line.startswith(PROMPT) for line in lines[-_SCREEN_TAIL_LINES:])

    def screen_reading(self, capture: str) -> ScreenReading:
        """Classify the rendered screen as trust, approval, working, or prompt.

        Arm order is load-bearing: both the trust dialog and the approval
        overlay render a selection row starting with `›`, so
        `is_idle_screen` returns True on both. The modal arms must therefore
        come before the `is_idle_screen` call, or both modals would classify
        as PROMPT and the send gate would inject into a live approval.
        """
        if TRUST_MARKER in capture:
            return ScreenReading(kind=ScreenKind.TRUST, confidence=ScreenConfidence.HIGH)
        if _in_screen_tail(capture, APPROVAL_MARKER):
            return ScreenReading(kind=ScreenKind.APPROVAL, confidence=ScreenConfidence.HIGH)
        if WORKING_MARKER in capture:
            return ScreenReading(kind=ScreenKind.WORKING, confidence=ScreenConfidence.HIGH)
        if self.is_idle_screen(capture):
            return ScreenReading(kind=ScreenKind.PROMPT, confidence=ScreenConfidence.HIGH)
        return ScreenReading(kind=ScreenKind.UNKNOWN, confidence=ScreenConfidence.LOW)
