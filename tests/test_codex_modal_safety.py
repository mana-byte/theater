"""Codex screen classification keeps trust menus out of the composer."""

from pathlib import Path

import pytest
from shipped import CodexObserver

from theater.harness.observation import ScreenConfidence, ScreenKind

SCREENS = Path(__file__).parent / "fixtures" / "screens"

#: Stock Codex 0.158 trust dialog: header plus a numbered option list whose
#: selected row renders with the composer's `›` glyph.
TRUST_MENU_0158 = """\
Codex CLI

Trust this folder?
Working with untrusted contents carries risk.

› 1. Trust and continue
  2. Start with restrictions

  Press enter to continue
"""

#: A restricted-folder variant keeps the 0.158 header but warns about the
#: restricted workspace in its own words.
TRUST_MENU_RESTRICTED = """\
Trust this folder?
This workspace contains restricted files.

› 1. Trust and continue
  2. Start with restrictions

  Press enter to continue
"""

#: A trust-family dialog whose header wording is not confirmed evidence:
#: it must never advertise a ready composer, known or not.
TRUST_MENU_UNCONFIRMED = """\
Open this workspace in restricted mode?

› 1. Trust and continue
  2. Start with restrictions

  Press enter to continue
"""


def _screen(name: str) -> str:
    return (SCREENS / f"{name}.txt").read_text()


@pytest.mark.parametrize(
    "capture",
    [TRUST_MENU_0158, TRUST_MENU_RESTRICTED],
    ids=["stock-0.158", "restricted-folder"],
)
def test_trust_menu_classifies_as_trust_not_prompt(capture):
    """The 0.158 trust menu must never advertise a ready composer."""
    observer = CodexObserver()
    reading = observer.screen_reading(capture)
    assert reading.kind is ScreenKind.TRUST
    assert reading.confidence is ScreenConfidence.HIGH
    assert observer.is_idle_screen(capture) is False


def test_unconfirmed_trust_menu_wording_is_not_a_prompt():
    """Unconfirmed dialog wording stays unknown; the send gate refuses it."""
    observer = CodexObserver()
    reading = observer.screen_reading(TRUST_MENU_UNCONFIRMED)
    assert reading.kind is ScreenKind.UNKNOWN
    assert reading.confidence is ScreenConfidence.LOW
    assert observer.is_idle_screen(TRUST_MENU_UNCONFIRMED) is False


@pytest.mark.parametrize("name", ["codex_idle", "codex_trust", "codex_approval"])
def test_existing_fixture_readings_are_preserved(name):
    """Real idle, old trust, and approval fixtures keep their readings."""
    capture = _screen(name)
    observer = CodexObserver()
    reading = observer.screen_reading(capture)
    expected = {
        "codex_idle": ScreenKind.PROMPT,
        "codex_trust": ScreenKind.TRUST,
        "codex_approval": ScreenKind.APPROVAL,
    }[name]
    assert reading.kind is expected
    assert reading.confidence is ScreenConfidence.HIGH
    assert observer.is_idle_screen(capture) is (expected is ScreenKind.PROMPT)


def test_numbered_menu_row_is_not_a_prompt():
    """A `› 1. …` selection row is a menu option, never the composer."""
    capture = "\n".join(
        [
            "  Approval settings",
            "",
            "› 1. Always ask",
            "  2. Auto-approve edits",
            "",
            "Press enter to confirm or esc to go back",
        ]
    )
    observer = CodexObserver()
    assert observer.is_idle_screen(capture) is False
    reading = observer.screen_reading(capture)
    assert reading.kind is ScreenKind.UNKNOWN
    assert reading.confidence is ScreenConfidence.LOW


#: Assistant prose quoting the complete menu, with the live composer below.
QUOTED_MENU_THEN_COMPOSER = """\
The dialog looks like this:
Trust this folder?
› 1. Trust and continue
  2. Start with restrictions
Press enter to continue

› Ask Codex to do anything
"""

#: A composer row above a live modal is scrollback; the modal still wins.
COMPOSER_THEN_LIVE_MENU = """\
› Ask Codex to do anything

Trust this folder?
› 1. Trust and continue
  2. Start with restrictions

Press enter to continue
"""


def test_quoted_menu_with_later_composer_is_prompt():
    """Prose quoting full dialog chrome must not outrank the live composer."""
    observer = CodexObserver()
    reading = observer.screen_reading(QUOTED_MENU_THEN_COMPOSER)
    assert reading.kind is ScreenKind.PROMPT
    assert reading.confidence is ScreenConfidence.HIGH
    assert observer.is_idle_screen(QUOTED_MENU_THEN_COMPOSER) is True


def test_composer_above_a_live_modal_does_not_override_it():
    """Scrollback composer text never dismisses a modal below it."""
    observer = CodexObserver()
    reading = observer.screen_reading(COMPOSER_THEN_LIVE_MENU)
    assert reading.kind is ScreenKind.TRUST
    assert reading.confidence is ScreenConfidence.HIGH
    assert observer.is_idle_screen(COMPOSER_THEN_LIVE_MENU) is False


@pytest.mark.parametrize("composer_below", [False, True])
def test_unknown_menu_must_not_reuse_an_earlier_composer(composer_below):
    menu = "Approval settings\n› 1. Always ask\n2. Auto-approve edits\nPress enter to confirm"
    capture = f"{menu}\n› Ask Codex" if composer_below else f"› Ask Codex\n{menu}"
    observer = CodexObserver()

    reading = observer.screen_reading(capture)

    assert reading.kind is (ScreenKind.PROMPT if composer_below else ScreenKind.UNKNOWN)
    assert reading.confidence is (ScreenConfidence.HIGH if composer_below else ScreenConfidence.LOW)
    assert observer.is_idle_screen(capture) is composer_below


def test_prose_quoting_a_trust_header_is_not_a_trust_dialog():
    """Assistant prose quoting a trust header, with a live composer, is a prompt."""
    capture = "\n".join(
        [
            "The agent asked: Do you trust the contents of this directory?",
            "Then it kept summarising the review.",
            "",
            "› Write more tests",
        ]
    )
    observer = CodexObserver()
    reading = observer.screen_reading(capture)
    assert reading.kind is ScreenKind.PROMPT
    assert reading.confidence is ScreenConfidence.HIGH
    assert observer.is_idle_screen(capture) is True
