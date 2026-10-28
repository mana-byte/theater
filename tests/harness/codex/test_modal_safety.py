"""Codex screen classification keeps trust menus out of the composer."""

import pytest

from tests.support.shipped import CodexObserver
from theater.harness.observation import ScreenConfidence, ScreenKind

#: Stock trust dialog (tui/src/onboarding/trust_directory.rs, in-window tags).
TRUST_MENU_STOCK = """\
Trust this folder? Codex can read, edit, and run files here, subject to your permission \
settings. Folder settings can run code automatically, even without a model request. Continue \
only if you trust these files. Your trust decision will be saved.

\u203a 1. Trust and continue
  2. Quit

  enter continue \u00b7 esc quit
"""

#: The restricted-open dialog keeps the numbered chrome but never the trust
#: header; its wording must never advertise a ready composer, known or not.
TRUST_MENU_RESTRICTED = """\
Config, hooks, and exec policies from untrusted folders stay disabled. Trusted project \
folders can still contribute settings. Skills still load, and tools follow your permission \
settings. Opening will not change saved trust.

\u203a 1. Open restricted
  2. Quit

  enter continue \u00b7 esc quit
"""

#: A trust-family dialog whose header wording is not confirmed evidence:
#: it must never advertise a ready composer, known or not.
TRUST_MENU_UNCONFIRMED = """\
Open this workspace in restricted mode?

\u203a 1. Trust and continue
  2. Quit

  enter continue \u00b7 esc quit
"""


def test_trust_menu_classifies_as_trust_not_prompt():
    """The stock trust menu must never advertise a ready composer."""
    observer = CodexObserver()
    reading = observer.screen_reading(TRUST_MENU_STOCK)
    assert reading.kind is ScreenKind.TRUST
    assert reading.confidence is ScreenConfidence.HIGH
    assert observer.is_idle_screen(TRUST_MENU_STOCK) is False


@pytest.mark.parametrize(
    "capture",
    [TRUST_MENU_RESTRICTED, TRUST_MENU_UNCONFIRMED],
    ids=["restricted-open", "unconfirmed"],
)
def test_unconfirmed_trust_menu_wording_is_not_a_prompt(capture):
    """Dialog wording without the trust header stays unknown; the send gate refuses it."""
    observer = CodexObserver()
    reading = observer.screen_reading(capture)
    assert reading.kind is ScreenKind.UNKNOWN
    assert reading.confidence is ScreenConfidence.LOW
    assert observer.is_idle_screen(capture) is False


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
\u203a 1. Trust and continue
  2. Quit
enter continue \u00b7 esc quit

\u203a Ask Codex to do anything
"""

#: A composer row above a live modal is scrollback; the modal still wins.
COMPOSER_THEN_LIVE_MENU = """\
\u203a Ask Codex to do anything

Trust this folder?
\u203a 1. Trust and continue
  2. Quit

enter continue \u00b7 esc quit
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


@pytest.mark.parametrize("padding", [1, 6])
def test_approval_suffix_keeps_its_original_footer_window(padding):
    capture = "The dialog says to cancel\n" + "output\n" * padding + "esc to interrupt"
    reading = CodexObserver().screen_reading(capture)

    assert reading.kind is (ScreenKind.APPROVAL if padding == 1 else ScreenKind.WORKING)
    assert reading.confidence is ScreenConfidence.HIGH


def test_prose_quoting_a_trust_header_is_not_a_trust_dialog():
    """Assistant prose quoting a trust header, with a live composer, is a prompt."""
    capture = "\n".join(
        [
            "The agent asked: Trust this folder?",
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
