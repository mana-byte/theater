"""OpenCode source text, usage and screens read to the expected events and readings."""

from __future__ import annotations

from tests.support.parse_rig import read_screen
from tests.support.shipped import OpenCodeObserver
from theater.harness.base import MAX_TEXT
from theater.harness.observation import ScreenConfidence, ScreenKind

LONG = "x" * (MAX_TEXT * 3)


def test_opencode_source_preserves_raw_text_before_clipping(tmp_path):
    from theater.harness.builtin.plugins.opencode import OpenCodeSource

    src = OpenCodeSource(tmp_path / "opencode-stable.db", cwd=str(tmp_path))
    src._roles["msg_a1"] = "assistant"
    src._text["msg_a1"] = {"prt_1": LONG}

    events = src._on_message(
        {
            "info": {
                "id": "msg_a1",
                "role": "assistant",
                "finish": "stop",
                "time": {"created": 1000, "completed": 2000},
            }
        },
        0,
    )

    assert events[0].text != LONG
    assert events[0].text.endswith(f"(+{MAX_TEXT * 2} chars)")
    assert events[0].raw_text == LONG


def test_opencode_emits_usage_for_an_empty_tool_step(tmp_path):
    from theater.harness.builtin.plugins.opencode import OpenCodeSource

    src = OpenCodeSource(tmp_path / "opencode-stable.db", cwd=str(tmp_path))
    events = src._on_message(
        {
            "info": {
                "id": "msg_a1",
                "role": "assistant",
                "finish": "tool-calls",
                "modelID": "model-a",
                "time": {"completed": 2000},
                "cost": 0.01,
                "tokens": {
                    "input": 10,
                    "output": 4,
                    "reasoning": 2,
                    "cache": {"read": 3, "write": 1},
                },
            }
        },
        0,
    )

    assert len(events) == 1
    usage = events[0].usage
    assert usage is not None
    assert usage.idempotency_key == "opencode:msg_a1"
    assert usage.input_tokens == 10
    assert usage.cache_read_input_tokens == 3
    assert events[0].turn_end is False


def test_opencode_reported_terminal_signal_remains_unmarked(tmp_path):
    from theater.harness.builtin.plugins.opencode import OpenCodeSource

    src = OpenCodeSource(tmp_path / "opencode-stable.db", cwd=str(tmp_path))
    src._snapshotted.add("msg_a1")
    events = src._on_message({"info": {"id": "msg_a1", "role": "assistant", "finish": "stop"}}, 1)
    assert len(events) == 1
    assert events[0].turn_end is True
    assert events[0].text == events[0].raw_text == ""
    assert events[0].answer_final is False


def test_opencode_idle_fixture_classifies_as_prompt():
    capture = read_screen("opencode_idle.txt")
    reading = OpenCodeObserver().screen_reading(capture)
    assert reading.kind is ScreenKind.PROMPT


def test_opencode_working_fixture_classifies_as_working_with_high_confidence():
    capture = read_screen("opencode_working.txt")
    reading = OpenCodeObserver().screen_reading(capture)
    assert reading.kind is ScreenKind.WORKING
    assert reading.confidence is ScreenConfidence.HIGH


def test_opencode_working_interrupt_fixture_classifies_as_working_with_high_confidence():
    """After one Esc press, the footer reads `esc again to interrupt` for ~5s
    (component/prompt/index.tsx:410-418). The plain `esc interrupt` marker
    misses this spelling, so a working pane mid-interrupt falls through to the
    tail scan, reads PROMPT -> IDLE, and `_rescue_jobs` finishes the caller's
    job mid-turn."""
    capture = read_screen("opencode_working_interrupt.txt")
    assert "esc again to interrupt" in capture
    assert "esc interrupt" not in capture.split("esc again to interrupt")[0][-1:]
    reading = OpenCodeObserver().screen_reading(capture)
    assert reading.kind is ScreenKind.WORKING
    assert reading.confidence is ScreenConfidence.HIGH


def test_opencode_approval_fixture_classifies_as_approval_with_high_confidence():
    capture = read_screen("opencode_approval.txt")
    assert "Permission required" in capture
    assert "esc interrupt" not in capture
    assert "ctrl+p commands" not in capture
    reading = OpenCodeObserver().screen_reading(capture)
    assert reading.kind is ScreenKind.APPROVAL
    assert reading.confidence is ScreenConfidence.HIGH


def test_opencode_question_fixture_classifies_as_approval_with_high_confidence():
    """The question screen (`routes/session/question.tsx`) renders a footer
    `esc dismiss` and no prompt chrome. It classifies as APPROVAL (HIGH) — not
    a new ScreenKind — because it is functionally an approval: the agent is
    blocked and Enter commits a choice. The send gate blocks APPROVAL at HIGH
    confidence, so classifying it as APPROVAL prevents injection."""
    capture = read_screen("opencode_question.txt")
    assert "esc dismiss" in capture
    assert "esc interrupt" not in capture
    assert "ctrl+p commands" not in capture
    reading = OpenCodeObserver().screen_reading(capture)
    assert reading.kind is ScreenKind.APPROVAL
    assert reading.confidence is ScreenConfidence.HIGH


def test_opencode_agent_prose_containing_esc_interrupt_is_not_working():
    """BOUNDARY regression: agent prose containing `esc interrupt` sitting on
    the line immediately above the composer footer, with nothing between, on an
    otherwise idle pane. The working marker must be tail-scoped with a
    co-occurrence guard (the footer line has both `esc interrupt` and
    `ctrl+p commands`; prose has only `esc interrupt`), or the pane reads
    WORKING and `_rescue_jobs` never sees a PROMPT."""
    capture = (
        "  The esc interrupt marker is in component/prompt/index.tsx\n  tab agents  ctrl+p commands"
    )
    assert "esc interrupt" in capture
    reading = OpenCodeObserver().screen_reading(capture)
    assert reading.kind is not ScreenKind.WORKING
    assert reading.kind is ScreenKind.PROMPT


def test_opencode_agent_prose_containing_permission_required_is_not_approval():
    """ECHO regression: an idle pane whose output contains the literal text
    `Permission required` (as if the agent printed the fixture) while the
    composer footer `ctrl+p commands` is still visible. The modal arms are
    gated on the absence of all prompt-component chrome: when the prompt is
    visible, `Permission required` in prose must not classify as APPROVAL —
    that would make the pane unreachable through the send gate."""
    capture = "  The marker is Permission required in permission.tsx\n  tab agents  ctrl+p commands"
    assert "Permission required" in capture
    reading = OpenCodeObserver().screen_reading(capture)
    assert reading.kind is not ScreenKind.APPROVAL
    assert reading.kind is ScreenKind.PROMPT


def test_opencode_aborted_turn_is_not_working():
    """ABORT regression: an idle pane containing `· interrupted` in the
    message log (`routes/session/index.tsx:1569`). `interrupt` is a substring
    of `interrupted`, so shortening the marker to `"interrupt"` would read
    WORKING forever on an idle pane after an abort. The full spellings
    `esc interrupt` and `again to interrupt` are not substrings of
    `· interrupted`."""
    capture = "  assistant · interrupted\n  tab agents  ctrl+p commands"
    assert "interrupted" in capture
    reading = OpenCodeObserver().screen_reading(capture)
    assert reading.kind is not ScreenKind.WORKING
    assert reading.kind is ScreenKind.PROMPT
