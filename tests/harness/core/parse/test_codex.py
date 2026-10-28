"""Codex rollout records, discovery and screens parse to the expected events and readings."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support.shipped import CodexObserver
from theater.harness import EventKind, TurnTerminal, status_after
from theater.harness.observation import ScreenKind
from theater.models import Status


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
                    "payload": {
                        "session_id": "ignored",
                        "cwd": cwd,
                        "thread_source": "user",
                        "source": "cli",
                    },
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


def test_codex_after_excludes_transcripts_that_predate_the_participant(tmp_path):
    root = tmp_path / "sessions"
    day = root / "2026" / "08" / "12"
    day.mkdir(parents=True)
    project = tmp_path / "work"
    project.mkdir()
    path = day / "rollout-2026-08-12T13-40-50-abc.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "cwd": str(project),
                    "thread_source": "user",
                    "source": "cli",
                },
            }
        )
        + "\n"
    )

    observer = CodexObserver(root=root)
    assert observer.find_transcript(cwd=str(project), after=0) == path
    # A floor in the future can never be satisfied by an existing file. The
    # filename's timestamp is local and is never what gets compared.
    assert observer.find_transcript(cwd=str(project), after=4e9) is None


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


def test_codex_output_scrolled_past_the_composer_is_not_idle():
    """Beyond the tail window the pane is showing something else entirely."""
    trailing = "\n".join(["more output"] * 8)
    assert CodexObserver().is_idle_screen(CODEX_IDLE + "\n" + trailing) is False


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
