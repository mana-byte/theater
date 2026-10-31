"""Codex screen classification finds a running turn once its interrupt hint truncates away."""

import pytest

from tests.support.shipped import CodexObserver
from theater.harness.observation import ScreenConfidence, ScreenKind

FOOTER = """\

› Ask Codex to do anything

  Gpt 6 Astra xhigh · ~/Desktop/theater
  ← for agents · ? for shortcuts
"""

#: A live pane: the status row holds a long reasoning sentence truncated by `…`, so the
#: `(Ns • esc to interrupt)` suffix is gone.
RUNNING_TRUNCATED_ROW = (
    "• Explored\n  └ Read store.py\n\n"
    "• Right now, I'm considering the task and I could begin prototyping…\n"
    "  └ Tip: Use /export to save your conversation as Markdown.\n\n" + FOOTER
)

#: The same pane once the turn ended: the `Worked for` separator sits above the composer.
FINISHED = (
    "• Done.\n  └ Tip: Use /export to save your conversation as Markdown.\n\n"
    "  Worked for 5s • 09:32\n\n" + FOOTER
)


@pytest.mark.parametrize(
    ("capture", "kind"),
    [(RUNNING_TRUNCATED_ROW, ScreenKind.WORKING), (FINISHED, ScreenKind.PROMPT)],
    ids=["running", "finished"],
)
def test_status_row_without_interrupt_hint_is_working_only_while_running(capture, kind):
    reading = CodexObserver().screen_reading(capture)

    assert reading.kind is kind
    assert (CodexObserver().is_idle_screen(capture)) is (kind is ScreenKind.PROMPT)
    if kind is ScreenKind.PROMPT:
        assert reading.confidence is ScreenConfidence.HIGH
