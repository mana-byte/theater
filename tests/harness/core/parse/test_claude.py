"""Claude Code transcript records and screens parse to the expected events and readings."""

from __future__ import annotations

import json

import pytest

from tests.support.parse_rig import events_for, read_screen
from tests.support.paths import FIXTURES
from tests.support.shipped import ClaudeCodeObserver
from theater.harness import EventKind, status_after
from theater.harness.observation import ScreenConfidence, ScreenKind
from theater.models import Status


def test_claude_turn_parses_to_the_expected_event_sequence():
    events = events_for(ClaudeCodeObserver(), FIXTURES / "claude_code.jsonl")

    assert [e.kind for e in events] == [
        EventKind.USER,
        EventKind.ASSISTANT,
        EventKind.ASSISTANT,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.ASSISTANT,
        EventKind.ASSISTANT,
    ]
    assert len(events) == 7
    assert events[3].tool_name == "Read"
    assert events[-1].turn_end is True
    assert [e.turn_end for e in events[:-1]] == [False] * 6

    repeated = [events[i].usage for i in (1, 2, 3)]
    assert all(item is not None for item in repeated)
    assert len({item.idempotency_key for item in repeated if item is not None}) == 1
    assert events[5].text == ""
    assert events[5].usage.output_tokens == 3081


def test_claude_records_carry_their_own_timestamp():
    events = events_for(ClaudeCodeObserver(), FIXTURES / "claude_code.jsonl")
    assert all(e.ts is not None for e in events)
    # ISO-8601 with a Z suffix, parsed as UTC rather than local time.
    assert events[0].ts == pytest.approx(1782325575.348, abs=0.01)


def test_claude_raw_index_tracks_the_source_record():
    events = events_for(ClaudeCodeObserver(), FIXTURES / "claude_code.jsonl")
    assert [e.raw_index for e in events] == [0, 1, 2, 3, 4, 5, 6]


def test_a_thinking_only_record_never_ends_the_turn():
    """Its text half may follow in the same message; the empty half proves nothing."""
    record = {
        "type": "assistant",
        "message": {
            "stop_reason": "end_turn",
            "content": [{"type": "thinking", "thinking": "quietly"}],
        },
    }
    assert ClaudeCodeObserver().parse(json.dumps(record), 0) == []


@pytest.mark.parametrize(
    "stop_reason,ends",
    [("tool_use", False), (None, False), ("end_turn", True), ("max_tokens", True)],
)
def test_claude_turn_end_rule(stop_reason, ends):
    record = {
        "type": "assistant",
        "message": {
            "stop_reason": stop_reason,
            "content": [{"type": "text", "text": "hi"}],
        },
    }
    events = ClaudeCodeObserver().parse(json.dumps(record), 0)
    assert events[0].turn_end is ends


def test_claude_api_errors_become_error_events():
    record = {
        "type": "system",
        "subtype": "api_error",
        "level": "error",
        "error": "overloaded_error",
    }
    events = ClaudeCodeObserver().parse(json.dumps(record), 0)
    assert [e.kind for e in events] == [EventKind.ERROR]
    assert events[0].text == "overloaded_error"
    # A failed request leaves the agent waiting, not working.
    assert status_after(events[0]) is Status.IDLE


def test_claude_approval_fixture_classifies_as_approval_with_high_confidence():
    capture = read_screen("claude_approval.txt")
    reading = ClaudeCodeObserver().screen_reading(capture)
    assert reading.kind is ScreenKind.APPROVAL
    assert reading.confidence is ScreenConfidence.HIGH


def test_claude_idle_fixture_classifies_as_prompt():
    capture = read_screen("claude_idle.txt")
    reading = ClaudeCodeObserver().screen_reading(capture)
    assert reading.kind is ScreenKind.PROMPT


def test_claude_agents_footer_fixture_classifies_as_prompt():
    """The agent-switcher footer can replace Claude's usual shortcut hint."""
    capture = read_screen("claude_idle_agents_footer.txt")
    assert "? for shortcuts" not in capture
    reading = ClaudeCodeObserver().screen_reading(capture)
    assert reading.kind is ScreenKind.PROMPT
    assert reading.confidence is ScreenConfidence.HIGH


def test_claude_agents_footer_words_in_agent_prose_are_not_a_prompt():
    capture = "The documentation tells users to press ← for agents\n\n❯\u00a0"
    reading = ClaudeCodeObserver().screen_reading(capture)
    assert reading.kind is ScreenKind.UNKNOWN


def test_claude_bare_composer_is_not_a_prompt():
    """Claude keeps the composer visible throughout a working turn."""
    reading = ClaudeCodeObserver().screen_reading("working through it\n\n❯\u00a0")
    assert reading.kind is ScreenKind.UNKNOWN


def test_claude_manual_mode_agents_footer_classifies_as_prompt():
    capture = "\n".join(
        [
            "finished",
            "❯\u00a0",
            "  ⏸ manual mode on · PR #46559 · ← for agents",
        ]
    )
    reading = ClaudeCodeObserver().screen_reading(capture)
    assert reading.kind is ScreenKind.PROMPT


@pytest.mark.parametrize(
    ("footer"),
    [
        "⏵⏵ bypass permissions on (shift+tab to cycle)",
        "⏵⏵ accept edits on (shift+tab to cycle)",
        "⏸ plan mode on (shift+tab to cycle)",
        "⏸ manual mode on (shift+tab to cycle)",
        "⏵⏵ don't ask on (shift+tab to cycle)",
        "⏵⏵ auto mode on (shift+tab to cycle)",
        "⏵⏵ bypass permissions on",
    ],
)
def test_claude_mode_line_footer_family_classifies_as_prompt(footer):
    """Bottommost mode footer → PROMPT; residual: no marker means PROMPT."""
    capture = "\n".join(["finished", "❯\u00a0", f"  {footer}"])
    reading = ClaudeCodeObserver().screen_reading(capture)
    assert reading.kind is ScreenKind.PROMPT
    assert reading.confidence is ScreenConfidence.HIGH


def test_claude_impossible_symbol_indicator_pairing_is_not_a_prompt():
    """The pause symbol never renders with bypass permissions."""
    capture = "\n".join(["finished", "❯\u00a0", "  ⏸ bypass permissions on (shift+tab to cycle)"])
    reading = ClaudeCodeObserver().screen_reading(capture)
    assert reading.kind is ScreenKind.UNKNOWN


def test_claude_is_idle_screen_delegates_to_the_reading():
    """The boolean keeps every caller, through the typed reading."""
    observer = ClaudeCodeObserver()
    assert observer.is_idle_screen("\n> ") is True
    assert observer.is_idle_screen("\n  ⏸ plan mode on (shift+tab to cycle)") is True
    assert observer.is_idle_screen("working through it\n\n❯\u00a0") is False


def test_claude_working_marker_wins_over_bottommost_mode_footer():
    """Precedence: the working marker beats the family match."""
    capture = "\n".join(
        [
            "  ✻ Working… esc to interrupt",
            "❯\u00a0",
            "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
        ]
    )
    observer = ClaudeCodeObserver()
    assert observer.screen_reading(capture).kind is ScreenKind.WORKING
    assert not observer.is_idle_screen(capture)


def test_claude_mode_footer_quoted_in_prose_is_not_a_prompt():
    """Only the bottommost line matches, so mid-prose quotes stay UNKNOWN."""
    capture = "\n".join(
        ["  ⏵⏵ bypass permissions on (shift+tab to cycle)", "assistant continues", "❯\u00a0"]
    )
    observer = ClaudeCodeObserver()
    assert observer.screen_reading(capture).kind is ScreenKind.UNKNOWN
    assert not observer.is_idle_screen(capture)


def test_claude_working_accept_edits_footer_wins_over_agents_footer():
    """Working must win when the idle-looking suffix shares the same line."""
    capture = "\n".join(
        [
            "✶ Determining…",
            "❯\u00a0",
            "  ⏵⏵ accept edits on · esc to interrupt · ← for agents",
        ]
    )
    reading = ClaudeCodeObserver().screen_reading(capture)
    assert reading.kind is ScreenKind.WORKING
    assert reading.confidence is ScreenConfidence.HIGH


def test_claude_working_fixture_classifies_as_working_not_prompt():
    """The permission-mode indicator is on screen during a turn as well.

    `manual mode on` is drawn in the footer whether claude is idle or working,
    so a matcher keyed on it reads a working screen as a prompt. The
    discriminating segment is `esc to interrupt` / `? for shortcuts`.
    """
    capture = read_screen("claude_working.txt")
    assert "manual mode on" in capture  # the non-discriminating indicator
    reading = ClaudeCodeObserver().screen_reading(capture)
    assert reading.kind is ScreenKind.WORKING
    assert reading.confidence is ScreenConfidence.HIGH


def test_claude_bash_approval_fixture_classifies_as_approval():
    """A second dialog shape, to show the marker is chrome and not one phrase.

    The write-permission dialog and the bash-permission dialog ask different
    questions and offer different options; both are framed by the same
    `Esc to cancel` footer.
    """
    capture = read_screen("claude_approval_bash.txt")
    reading = ClaudeCodeObserver().screen_reading(capture)
    assert reading.kind is ScreenKind.APPROVAL
    assert reading.confidence is ScreenConfidence.HIGH


def test_claude_trust_fixture_classifies_as_trust_with_high_confidence():
    """The workspace-trust onboarding dialog has no approval footer.

    Without a dedicated ``TRUST`` arm it falls through to ``UNKNOWN``: the
    reducer leaves the status untouched and the send gate lets keystrokes
    through into a trust prompt, which can accept trust no human granted.
    """
    capture = read_screen("claude_trust.txt")
    reading = ClaudeCodeObserver().screen_reading(capture)
    assert reading.kind is ScreenKind.TRUST
    assert reading.confidence is ScreenConfidence.HIGH


def test_claude_trust_marker_does_not_match_remote_control_add_server_dialog():
    """The ``Trust this directory?`` dialog is a different screen.

    It is the remote-control add-server confirmation with options
    ``Yes, trust and add server`` / ``No, go back``. The workspace-trust
    marker ``Yes, I trust this folder`` must not fire here: matching this
    neighbour would be non-dangerous (both are genuinely awaiting input)
    but it conflates two distinct screens, and a looser marker — say a bare
    ``trust`` substring — would also catch the cloud-gateway TLS-pinning
    dialog.
    """
    capture = "\n".join(
        [
            " Trust this directory?",
            "",
            " Yes, trust and add server",
            " No, go back",
        ]
    )
    reading = ClaudeCodeObserver().screen_reading(capture)
    assert reading.kind is not ScreenKind.TRUST
