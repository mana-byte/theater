"""Claude Code transcript records and screens parse to the expected events and readings."""

from __future__ import annotations

import json

import pytest

from tests.support.claude_screens import CLAUDE_APPROVAL, CLAUDE_IDLE, CLAUDE_TRUST
from tests.support.claude_seal import claude_marker, claude_record
from tests.support.parse_rig import read_screen
from tests.support.shipped import ClaudeCodeObserver
from theater.harness import EventKind, status_after
from theater.harness.observation import ScreenConfidence, ScreenKind
from theater.models import Status

# One whole turn: user, thinking, text, tool_use, tool_result, thinking, text(end_turn), marker.


def _turn_lines() -> list[str]:
    usage = {
        "input_tokens": 3,
        "cache_creation_input_tokens": 28144,
        "cache_read_input_tokens": 0,
        "output_tokens": 221,
    }
    return [
        claude_record("u0", None, "user", message={"role": "user", "content": "read a.txt"}),
        claude_record(
            "a1",
            "u0",
            message={
                "id": "msg_a",
                "stop_reason": "tool_use",
                "usage": usage,
                "content": [{"type": "thinking", "thinking": "hm"}],
            },
        ),
        claude_record(
            "a2",
            "a1",
            message={
                "id": "msg_a",
                "stop_reason": "tool_use",
                "usage": usage,
                "content": [{"type": "text", "text": "let me read it"}],
            },
        ),
        claude_record(
            "a3",
            "a2",
            message={
                "id": "msg_a",
                "stop_reason": "tool_use",
                "usage": usage,
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Read",
                        "input": {"file_path": "a.txt"},
                    }
                ],
            },
        ),
        claude_record(
            "u1",
            "a3",
            "user",
            message={
                "role": "user",
                "content": [{"tool_use_id": "t1", "type": "tool_result", "content": "ok"}],
            },
        ),
        claude_record(
            "b1",
            "u1",
            message={
                "id": "msg_b",
                "stop_reason": "tool_use",
                "usage": {
                    "input_tokens": 1,
                    "cache_creation_input_tokens": 38875,
                    "output_tokens": 3081,
                },
                "content": [{"type": "thinking", "thinking": "hm"}],
            },
        ),
        claude_record(
            "b2",
            "b1",
            message={
                "id": "msg_c",
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 1,
                    "cache_creation_input_tokens": 305,
                    "output_tokens": 166,
                },
                "content": [{"type": "text", "text": "done"}],
            },
        ),
        claude_marker("m", "b2"),
    ]


def test_claude_turn_parses_to_the_expected_event_sequence():
    observer = ClaudeCodeObserver()
    events = [event for i, line in enumerate(_turn_lines()) for event in observer.parse(line, i)]

    assert [e.kind for e in events] == [
        EventKind.USER,
        EventKind.ASSISTANT,
        EventKind.ASSISTANT,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.ASSISTANT,
        EventKind.ASSISTANT,
        EventKind.ASSISTANT,
    ]
    assert events[3].tool_name == "Read"
    assert events[-1].turn_end is True
    assert [e.turn_end for e in events[:-1]] == [False] * 7

    repeated = [events[i].usage for i in (1, 2, 3)]
    assert all(item is not None for item in repeated)
    assert len({item.idempotency_key for item in repeated if item is not None}) == 1
    assert events[5].text == ""
    assert events[5].usage.output_tokens == 3081


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
    lines = [
        claude_record("u0", None, "user", message={"role": "user", "content": "go"}),
        claude_record(
            "a1",
            "u0",
            message={
                "id": "m1",
                "stop_reason": stop_reason,
                "content": [{"type": "text", "text": "hi"}],
            },
        ),
    ]
    # Only a turn that ended gets a marker; end_turn is proven by it, the others by their stop.
    if stop_reason == "end_turn":
        lines.append(claude_marker("k", "a1"))
    observer = ClaudeCodeObserver()
    events = [event for i, line in enumerate(lines) for event in observer.parse(line, i)]
    assert events[-1].turn_end is ends


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


def test_claude_approval_footer_classifies_as_approval_with_high_confidence():
    """The `Esc to cancel` footer frames every permission dialog shape: the
    write and bash dialogs ask different questions but share this footer."""
    reading = ClaudeCodeObserver().screen_reading(CLAUDE_APPROVAL)
    assert reading.kind is ScreenKind.APPROVAL
    assert reading.confidence is ScreenConfidence.HIGH


def test_claude_idle_footer_classifies_as_prompt():
    """The shortcut hint is the idle evidence when no bare composer line shows."""
    reading = ClaudeCodeObserver().screen_reading(CLAUDE_IDLE)
    assert reading.kind is ScreenKind.PROMPT
    assert reading.confidence is ScreenConfidence.HIGH


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


# A working screen read as a prompt finishes the caller's await mid-turn. Claude and vibe keep a
# prompt-like line on screen all turn; the inline marker-precedence tests pin the other side.
def test_claude_trust_dialog_classifies_as_trust_with_high_confidence():
    """The workspace-trust onboarding dialog has no approval footer.

    Without a dedicated ``TRUST`` arm it falls through to ``UNKNOWN``: the
    reducer leaves the status untouched and the send gate lets keystrokes
    through into a trust prompt, which can accept trust no human granted.
    """
    reading = ClaudeCodeObserver().screen_reading(CLAUDE_TRUST)
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
