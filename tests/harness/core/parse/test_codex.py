"""Codex rollout records, discovery and screens parse to the expected events and readings."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support.parse_rig import events_for, read_screen
from tests.support.paths import FIXTURES
from tests.support.shipped import CodexObserver
from theater.harness import EventKind, TurnTerminal, status_after
from theater.harness.observation import ScreenConfidence, ScreenKind
from theater.models import Status


def test_codex_turn_parses_to_the_expected_event_sequence():
    events = events_for(CodexObserver(), FIXTURES / "codex.jsonl")

    assert [e.kind for e in events] == [
        EventKind.USER,
        EventKind.ASSISTANT,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.TOOL_RESULT,
        EventKind.ASSISTANT,
    ]
    # session_meta, turn_context, reasoning and token_count produce nothing.
    assert events[2].tool_name == "exec"
    assert events[-1].turn_end is True
    assert [e.turn_end for e in events[:-1]] == [False] * 5


def test_codex_usage_is_normalized_and_stably_keyed():
    lines = (FIXTURES / "turn_codex.jsonl").read_text().splitlines()
    observer = CodexObserver()
    first = observer.parse(lines[9], 9)
    second = observer.parse(lines[9], 9)

    assert len(first) == len(second) == 1
    assert first[0].usage == second[0].usage
    usage = first[0].usage
    assert usage is not None
    assert usage.input_tokens == 3
    assert usage.cache_creation_input_tokens == 15635
    assert usage.output_tokens == 11
    assert usage.idempotency_key is not None


def test_codex_takes_the_reply_from_the_turn_boundary_not_the_final_message():
    """`task_complete` repeats the final agent_message, so only one may speak.

    It has to be the boundary record: the observer hands the turn-ending
    event's text back to whoever awaited the job.
    """
    events = events_for(CodexObserver(), FIXTURES / "codex.jsonl")
    final = [e for e in events if e.text == "<final answer 208 chars>"]
    assert len(final) == 1
    assert final[0].turn_end is True


def test_codex_commentary_is_kept_but_does_not_end_the_turn():
    events = events_for(CodexObserver(), FIXTURES / "codex.jsonl")
    assert events[1].text == "<commentary 112 chars>"
    assert status_after(events[1]) is Status.WORKING


def test_codex_tool_output_blocks_are_flattened():
    events = events_for(CodexObserver(), FIXTURES / "codex.jsonl")
    assert events[3].text == "Script completed\n<tool output 64 chars>"
    # The record carries only a call_id, and parse holds no state across lines.
    assert events[3].tool_name is None


def test_codex_mcp_calls_are_the_only_sight_of_theaters_own_tools():
    """They never appear as response_items, so this branch is load-bearing."""
    events = events_for(CodexObserver(), FIXTURES / "codex.jsonl")
    assert events[4].tool_name == "theater.list_participants"
    assert events[4].text == "<mcp result 41 chars>"


def test_codex_a_completed_task_marks_the_turn_completed():
    """task_complete is the success boundary: done, not an unknown outcome."""
    record = {
        "timestamp": "2026-08-12T11:37:50.379Z",
        "type": "event_msg",
        "payload": {
            "type": "task_complete",
            "last_agent_message": "all done",
        },
    }
    events = CodexObserver().parse(json.dumps(record), 0)
    assert len(events) == 1
    assert events[0].kind is EventKind.ASSISTANT
    assert events[0].turn_end is True
    assert events[0].turn_terminal is TurnTerminal.COMPLETED
    assert status_after(events[0]) is Status.IDLE


def test_codex_an_aborted_turn_ends_the_turn():
    """Otherwise a caller awaits a reply that a human has already cancelled."""
    record = {
        "timestamp": "2026-08-12T11:37:50.379Z",
        "type": "event_msg",
        "payload": {"type": "turn_aborted", "reason": "interrupted"},
    }
    events = CodexObserver().parse(json.dumps(record), 0)
    assert len(events) == 1
    assert events[0].kind is EventKind.ERROR
    assert events[0].text == "turn aborted: interrupted"
    assert events[0].turn_terminal is TurnTerminal.INTERRUPTED
    assert status_after(events[0]) is Status.IDLE


def test_codex_function_calls_parse_like_custom_tool_calls():
    """Two spellings of the same thing; both appear in one transcript."""
    call = {
        "type": "response_item",
        "payload": {"type": "function_call", "name": "wait", "call_id": "c1"},
    }
    output = {
        "type": "response_item",
        "payload": {
            "type": "function_call_output",
            "call_id": "c1",
            "output": [{"type": "input_text", "text": "done"}],
        },
    }
    observer = CodexObserver()
    assert observer.parse(json.dumps(call), 0)[0].tool_name == "wait"
    assert observer.parse(json.dumps(output), 1)[0].text == "done"


def test_codex_finds_a_transcript_by_cwd_and_by_session_id(tmp_path):
    root = tmp_path / "sessions"
    day = root / "2026" / "08" / "12"
    day.mkdir(parents=True)
    project = tmp_path / "work"
    project.mkdir()

    def meta(path: Path, cwd: str) -> None:
        path.write_text(
            json.dumps(
                {
                    "timestamp": "2026-08-12T11:40:50.100Z",
                    "type": "session_meta",
                    "payload": {"session_id": "ignored", "cwd": cwd},
                }
            )
            + "\n"
        )

    sid = "019ff5c6-717c-7a70-9ec4-66dd1f4d173e"
    target = day / f"rollout-2026-08-12T13-40-50-{sid}.jsonl"
    meta(target, str(project))
    meta(day / "rollout-2026-08-12T09-00-00-0000-other.jsonl", "/elsewhere")

    observer = CodexObserver(root=root)
    assert observer.find_transcript(cwd=str(project)) == target
    assert observer.find_transcript(cwd="/nowhere") is None
    # A known session id is an exact glob: no scan, no date guessing.
    assert observer.find_transcript(cwd="/nowhere", session_id=sid) == target


def test_codex_session_id_is_the_uuid_tail_of_the_filename(tmp_path):
    """The uuid keeps its own hyphens, so the timestamp is what anchors it."""
    sid = "019ff5c6-717c-7a70-9ec4-66dd1f4d173e"
    path = tmp_path / f"rollout-2026-08-12T13-40-50-{sid}.jsonl"
    assert CodexObserver().session_id(path) == sid
    assert CodexObserver().session_id(tmp_path / "not-a-rollout.jsonl") is None


def test_codex_after_excludes_transcripts_that_predate_the_participant(tmp_path):
    root = tmp_path / "sessions"
    day = root / "2026" / "08" / "12"
    day.mkdir(parents=True)
    project = tmp_path / "work"
    project.mkdir()
    path = day / "rollout-2026-08-12T13-40-50-abc.jsonl"
    path.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": str(project)}}) + "\n")

    observer = CodexObserver(root=root)
    assert observer.find_transcript(cwd=str(project), after=0) == path
    # A floor in the future can never be satisfied by an existing file. The
    # filename's timestamp is local and is never what gets compared.
    assert observer.find_transcript(cwd=str(project), after=4e9) is None


def test_codex_has_no_native_children(tmp_path):
    """Codex has no sub-agent mechanism, so this is a statement, not a stub."""
    assert CodexObserver().native_children(tmp_path / "anything.jsonl") == []


#: Codex keeps a status footer under the composer, so its prompt is never the
#: bottom line and the shared "last line is the prompt" tests do not apply.
CODEX_IDLE = "\n".join(
    [
        "> Ran ls",
        "",
        "\u203aExplain this codebase",
        "gpt-5.6-sol medium \u00b7 ~/work",
    ]
)


CODEX_WORKING = "\n".join(
    [
        "> Ran ls",
        "",
        "\u2022 Working (12s \u2022 esc to interrupt)",
    ]
)


def test_codex_is_idle_when_the_composer_is_above_the_footer():
    assert CodexObserver().is_idle_screen(CODEX_IDLE) is True


def test_codex_is_not_idle_while_a_turn_is_running():
    """The composer is still on screen mid-turn; the status line is the tell."""
    assert CodexObserver().is_idle_screen(CODEX_WORKING) is False
    assert CodexObserver().is_idle_screen(CODEX_IDLE + "\n" + CODEX_WORKING) is False


def test_codex_output_scrolled_past_the_composer_is_not_idle():
    """Beyond the tail window the pane is showing something else entirely."""
    trailing = "\n".join(["more output"] * 8)
    assert CodexObserver().is_idle_screen(CODEX_IDLE + "\n" + trailing) is False


def test_codex_idle_fixture_classifies_as_prompt():
    capture = read_screen("codex_idle.txt")
    reading = CodexObserver().screen_reading(capture)
    assert reading.kind is ScreenKind.PROMPT


def test_codex_approval_fixture_classifies_as_approval_with_high_confidence():
    """The approval overlay renders a `›` selection row, so `is_idle_screen`
    returns True on it. The APPROVAL_MARKER (`to cancel`) must be checked
    before the idle fallthrough, or the send gate would inject into a live
    approval modal."""
    capture = read_screen("codex_approval.txt")
    reading = CodexObserver().screen_reading(capture)
    assert reading.kind is ScreenKind.APPROVAL
    assert reading.confidence is ScreenConfidence.HIGH


def test_codex_trust_fixture_classifies_as_trust_with_high_confidence():
    """The trust dialog also renders a `›` selection row. TRUST_MARKER must
    be checked before the idle fallthrough for the same reason as approval."""
    capture = read_screen("codex_trust.txt")
    reading = CodexObserver().screen_reading(capture)
    assert reading.kind is ScreenKind.TRUST
    assert reading.confidence is ScreenConfidence.HIGH


def test_codex_approvals_settings_popup_is_not_an_approval():
    """The `/approvals` settings popup renders `to confirm or … to go back`.
    Keying on `to confirm` would wrongly classify it as an approval; the
    marker is `to cancel`, which that popup does not render."""
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
    reading = CodexObserver().screen_reading(capture)
    assert reading.kind is not ScreenKind.APPROVAL


def test_codex_agent_output_containing_to_cancel_is_not_an_approval():
    """The APPROVAL_MARKER substring `to cancel` is deliberately loose for
    keymap-independence. A whole-capture match let agent prose impersonate the
    approval footer, making the pane unreachable. The marker is tail-scoped
    AND endswith-anchored: the footer is a whole line ending with the marker,
    while prose containing the phrase mid-line does not. This is the boundary
    case — the phrase is on the line immediately above the composer, inside
    the tail window — and it must still classify as PROMPT, not APPROVAL."""
    capture = "\n".join(
        [
            "  I looked at the checkout flow. The Stripe webhook has no way",
            "  to cancel an in-flight payment intent, so we need to add one.",
            "\u203a ",
            "  gpt-5.5 high \u00b7 /tmp/example",
        ]
    )
    reading = CodexObserver().screen_reading(capture)
    assert reading.kind is not ScreenKind.APPROVAL
    assert reading.kind is ScreenKind.PROMPT
