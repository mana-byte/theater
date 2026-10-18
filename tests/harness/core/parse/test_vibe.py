"""Vibe transcript records, discovery and screens parse to the expected events and readings."""

from __future__ import annotations

import json

import pytest

from tests.support.parse_rig import events_for, read_screen
from tests.support.paths import FIXTURES
from tests.support.shipped import VibeObserver
from theater.harness import EventKind
from theater.harness.observation import ScreenConfidence, ScreenKind


def test_vibe_records_parse_to_the_expected_event_sequence():
    events = events_for(VibeObserver(), FIXTURES / "vibe_messages.jsonl")

    assert [e.kind for e in events] == [
        EventKind.USER,
        EventKind.ASSISTANT,
        EventKind.ASSISTANT,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
    ]
    # An assistant record without tool_calls is the end of a turn.
    assert events[1].turn_end is True
    # One with tool_calls is not, and neither is the call it emits.
    assert events[2].turn_end is False
    assert events[3].turn_end is False
    assert events[3].tool_name == "read_file"
    # Unlike Claude Code, Vibe names the tool on the result too.
    assert events[4].tool_name == "read_file"


@pytest.mark.parametrize("calls", [None, []])
def test_vibe_falsy_tool_calls_mean_the_turn_ended(calls):
    """Observed only as an absent key, but absent and empty must agree."""
    record = {"role": "assistant", "content": "done", "tool_calls": calls}
    events = VibeObserver().parse(json.dumps(record), 0)
    assert len(events) == 1
    assert events[0].turn_end is True


def test_vibe_tool_call_turn_with_no_content_emits_only_the_call():
    record = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "x", "function": {"name": "bash", "arguments": "{}"}}],
    }
    events = VibeObserver().parse(json.dumps(record), 0)
    assert [e.kind for e in events] == [EventKind.TOOL_CALL]
    assert events[0].tool_name == "bash"


def test_vibe_finds_a_transcript_through_meta_json(tmp_path):
    root = tmp_path / "session"
    project = tmp_path / "work"
    project.mkdir()
    older = root / "session_20260101_000000_aaaaaaaa"
    newer = root / "session_20260102_000000_bbbbbbbb"
    for d, cwd in ((older, project), (newer, project)):
        d.mkdir(parents=True)
        (d / "messages.jsonl").write_text("")
        (d / "meta.json").write_text(
            json.dumps(
                {
                    "session_id": f"{d.name[-8:]}-1111-2222-3333",
                    "environment": {"working_directory": str(cwd)},
                }
            )
        )

    observer = VibeObserver(root=root)
    # Newest first: directory names sort chronologically.
    assert observer.find_transcript(cwd=str(project)) == newer / "messages.jsonl"
    assert (
        observer.find_transcript(cwd=str(project), session_id="aaaaaaaa-1111-2222-3333")
        == older / "messages.jsonl"
    )
    assert observer.session_id(newer / "messages.jsonl") == "bbbbbbbb-1111-2222-3333"


def test_vibe_ignores_directories_without_a_transcript(tmp_path):
    root = tmp_path / "session"
    project = tmp_path / "work"
    project.mkdir()
    d = root / "session_20260101_000000_aaaaaaaa"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"environment": {"working_directory": str(project)}}))
    assert VibeObserver(root=root).find_transcript(cwd=str(project)) is None


def test_vibe_reports_the_sub_agents_it_spawned_itself():
    children = VibeObserver().native_children(FIXTURES / "vibe_messages.jsonl")
    # The fixture transcript's own directory has no meta.json next to it.
    assert children == []


def test_vibe_native_children_come_from_meta_json(tmp_path):
    session = tmp_path / "session_20260101_000000_aaaaaaaa"
    session.mkdir()
    (session / "messages.jsonl").write_text("")
    (session / "meta.json").write_text((FIXTURES / "vibe_meta.json").read_text())

    children = VibeObserver().native_children(session / "messages.jsonl")
    assert len(children) == 1
    assert children[0].agent == "explore"
    assert children[0].session_id == "9edd3dbf-b456-76cf-2f16-ea386d9c5cf2"
    assert children[0].relative_path is not None
    assert children[0].relative_path.startswith("agents/explore_")


def test_vibe_approval_fixture_classifies_as_approval_not_working():
    """The working marker is present in the same capture as the permission box.

    This is the regression test for the rule that approval must be tested
    before the working marker: vibe renders both simultaneously, so a matcher
    that checks `working` first would classify every approval dialog as
    `working` and the bug survives.
    """
    capture = read_screen("vibe_approval.txt")
    assert "esc" in capture.lower()  # the working spinner is on screen
    reading = VibeObserver().screen_reading(capture)
    assert reading.kind is ScreenKind.APPROVAL
    assert reading.kind is not ScreenKind.WORKING
    assert reading.confidence is ScreenConfidence.HIGH


def test_vibe_idle_fixture_classifies_as_prompt():
    capture = read_screen("vibe_idle.txt")
    reading = VibeObserver().screen_reading(capture)
    assert reading.kind is ScreenKind.PROMPT


def test_vibe_working_fixture_classifies_as_working_not_prompt():
    """The composer's empty prompt line stays on screen during a turn.

    A tail scan for a bare prompt therefore matches while vibe is working, so
    the working marker has to be tested first.
    """
    capture = read_screen("vibe_working.txt")
    reading = VibeObserver().screen_reading(capture)
    assert reading.kind is ScreenKind.WORKING
    assert reading.confidence is ScreenConfidence.HIGH


def test_vibe_trust_fixture_classifies_as_trust_with_high_confidence():
    """The trust dialog renders before any turn or prompt and blocks all
    interaction, so it must be classified as TRUST — the send gate refuses to
    inject on a TRUST reading at HIGH confidence."""
    capture = read_screen("vibe_trust.txt")
    reading = VibeObserver().screen_reading(capture)
    assert reading.kind is ScreenKind.TRUST
    assert reading.confidence is ScreenConfidence.HIGH


def test_vibe_working_queued_fixture_classifies_as_working_not_prompt():
    """The queued-message spinner hint spells `Esc to interrupt`, not
    `Esc/Ctrl+C to interrupt`. The composer's empty prompt line is still
    visible below the spinner, so without a matching WORKING_MARKER this
    screen reads as PROMPT -> IDLE and `_rescue_jobs` ends the caller's job
    mid-turn. This test must fail before the marker fix and pass after."""
    capture = read_screen("vibe_working_queued.txt")
    assert "Esc to interrupt" in capture  # the queued variant
    assert "Esc/Ctrl+C to interrupt" not in capture  # NOT the plain variant
    reading = VibeObserver().screen_reading(capture)
    assert reading.kind is ScreenKind.WORKING
    assert reading.kind is not ScreenKind.PROMPT
    assert reading.confidence is ScreenConfidence.HIGH


@pytest.mark.parametrize("cancel", ["Cancel", "cancel"])
def test_vibe_picker_footer_esc_cancel_is_not_approval(cancel):
    """Picker navigation alone is not evidence of an agent input request."""
    capture = (
        "  Session picker\n"
        "  › recent session\n"
        "    older session\n"
        "\n"
        f"↑↓/jk navigate  Enter select  Esc {cancel}\n"
    )
    reading = VibeObserver().screen_reading(capture)
    assert reading.kind is not ScreenKind.APPROVAL


@pytest.mark.parametrize("select", ["select", "toggle"])
def test_vibe_question_requires_current_dialog_and_input_status(select):
    capture = read_screen("vibe_question.txt").replace("Enter select", f"Enter {select}")
    reading = VibeObserver().screen_reading(capture)
    assert reading.kind is ScreenKind.APPROVAL
    assert reading.confidence is ScreenConfidence.HIGH
    for neighbor in (
        capture.replace("User input required…", "Generating…"),
        capture.replace("⠐⠋ User input required…", "Quoted text: User input required…"),
        capture + "\n> ",
        capture + "\n" * 10 + "> ",
    ):
        assert VibeObserver().screen_reading(neighbor).kind is not ScreenKind.APPROVAL


def test_vibe_agent_output_containing_to_interrupt_is_not_working():
    """The whole-capture match made agent output impersonate the spinner.

    `to interrupt` is ordinary English prose that an agent can echo in its
    own output. A whole-capture (or even tail-scoped) match reads an idle
    pane as WORKING, so `_rescue_jobs` never sees a PROMPT and the caller's
    job hangs. The working arm requires `Esc` to co-occur on the same line,
    which the spinner always does and prose never does.

    This probes the boundary: the prose sits on the line immediately above
    the composer, with nothing between — the case where tail-scoping alone
    still fails.
    """
    capture = "  I added a signal handler to interrupt the loop.\n> "
    assert "to interrupt" in capture
    reading = VibeObserver().screen_reading(capture)
    assert reading.kind is not ScreenKind.WORKING
