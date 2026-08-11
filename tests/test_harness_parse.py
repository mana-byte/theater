"""Parsing real transcript records into normalized events.

The fixtures are structurally faithful slices of actual Claude Code and Vibe
transcripts: every key and every discriminator (record type, role, stop_reason,
presence of tool_calls) is preserved, while free text is replaced with
placeholders so the repository carries no private content. Shape is what the
parser reads; content is what it clips.

tests/fixtures/claude_code.jsonl is one whole turn, in transcript order:
    user, thinking, text, tool_use, tool_result, thinking, text(end_turn), system
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from theater.harness import EventKind, status_after
from theater.harness.base import MAX_TEXT
from theater.harness.claude_code import ClaudeCodeHarness
from theater.harness.vibe import VibeHarness
from theater.models import Status

FIXTURES = Path(__file__).parent / "fixtures"


def events_for(harness, path: Path):
    out = []
    for i, line in enumerate(path.read_text().splitlines()):
        out.extend(harness.parse(line, i))
    return out


# ---- claude code -------------------------------------------------------


def test_claude_turn_parses_to_the_expected_event_sequence():
    events = events_for(ClaudeCodeHarness(), FIXTURES / "claude_code.jsonl")

    assert [e.kind for e in events] == [
        EventKind.USER,
        EventKind.ASSISTANT,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.ASSISTANT,
    ]
    # Thinking blocks and bookkeeping records produce nothing at all.
    assert len(events) == 5
    assert events[2].tool_name == "Read"
    assert events[-1].turn_end is True
    assert [e.turn_end for e in events[:-1]] == [False] * 4


def test_claude_records_carry_their_own_timestamp():
    events = events_for(ClaudeCodeHarness(), FIXTURES / "claude_code.jsonl")
    assert all(e.ts is not None for e in events)
    # ISO-8601 with a Z suffix, parsed as UTC rather than local time.
    assert events[0].ts == pytest.approx(1782325575.348, abs=0.01)


def test_claude_raw_index_tracks_the_source_record():
    events = events_for(ClaudeCodeHarness(), FIXTURES / "claude_code.jsonl")
    assert [e.raw_index for e in events] == [0, 2, 3, 4, 6]


def test_a_thinking_only_record_still_ends_the_turn():
    """The boundary must survive the block being filtered out."""
    record = {
        "type": "assistant",
        "message": {
            "stop_reason": "end_turn",
            "content": [{"type": "thinking", "thinking": "quietly"}],
        },
    }
    events = ClaudeCodeHarness().parse(json.dumps(record), 0)
    assert len(events) == 1
    assert events[0].kind is EventKind.ASSISTANT
    assert events[0].turn_end is True
    assert events[0].text == ""


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
    events = ClaudeCodeHarness().parse(json.dumps(record), 0)
    assert events[0].turn_end is ends


def test_claude_api_errors_become_error_events():
    record = {
        "type": "system",
        "subtype": "api_error",
        "level": "error",
        "error": "overloaded_error",
    }
    events = ClaudeCodeHarness().parse(json.dumps(record), 0)
    assert [e.kind for e in events] == [EventKind.ERROR]
    assert events[0].text == "overloaded_error"
    # A failed request leaves the agent waiting, not working.
    assert status_after(events[0]) is Status.IDLE


def test_claude_finds_a_transcript_by_cwd_not_by_slug(tmp_path):
    """The directory name is lossy; the cwd inside the records is not."""
    root = tmp_path / "projects"
    wanted = root / "-some-mangled-name"
    wanted.mkdir(parents=True)
    other = root / "-another"
    other.mkdir()
    project = tmp_path / "work"
    project.mkdir()

    (other / "aaa.jsonl").write_text(
        json.dumps({"type": "user", "cwd": "/elsewhere", "message": {}}) + "\n"
    )
    target = wanted / "bbb-session-id.jsonl"
    target.write_text(
        json.dumps({"type": "permission-mode"}) + "\n"
        + json.dumps({"type": "user", "cwd": str(project), "message": {}}) + "\n"
    )

    harness = ClaudeCodeHarness(root=root)
    assert harness.find_transcript(cwd=str(project)) == target
    assert harness.find_transcript(cwd="/nowhere") is None
    # A known session id is an exact filename lookup, no scan.
    assert (
        harness.find_transcript(cwd="/nowhere", session_id="bbb-session-id") == target
    )
    assert harness.session_id(target) == "bbb-session-id"


def test_claude_after_excludes_transcripts_that_predate_the_participant(tmp_path):
    root = tmp_path / "projects"
    d = root / "-p"
    d.mkdir(parents=True)
    project = tmp_path / "work"
    project.mkdir()
    path = d / "s.jsonl"
    path.write_text(json.dumps({"type": "user", "cwd": str(project), "message": {}}) + "\n")

    harness = ClaudeCodeHarness(root=root)
    assert harness.find_transcript(cwd=str(project), after=0) == path
    # A floor in the future can never be satisfied by an existing file.
    assert harness.find_transcript(cwd=str(project), after=4e9) is None


# ---- vibe --------------------------------------------------------------


def test_vibe_records_parse_to_the_expected_event_sequence():
    events = events_for(VibeHarness(), FIXTURES / "vibe_messages.jsonl")

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


def test_vibe_events_carry_no_timestamp():
    """Not an omission here: messages.jsonl records no time at all."""
    events = events_for(VibeHarness(), FIXTURES / "vibe_messages.jsonl")
    assert all(e.ts is None for e in events)


@pytest.mark.parametrize("calls", [None, []])
def test_vibe_falsy_tool_calls_mean_the_turn_ended(calls):
    """Observed only as an absent key, but absent and empty must agree."""
    record = {"role": "assistant", "content": "done", "tool_calls": calls}
    events = VibeHarness().parse(json.dumps(record), 0)
    assert len(events) == 1
    assert events[0].turn_end is True


def test_vibe_tool_call_turn_with_no_content_emits_only_the_call():
    record = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "x", "function": {"name": "bash", "arguments": "{}"}}],
    }
    events = VibeHarness().parse(json.dumps(record), 0)
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

    harness = VibeHarness(root=root)
    # Newest first: directory names sort chronologically.
    assert harness.find_transcript(cwd=str(project)) == newer / "messages.jsonl"
    assert (
        harness.find_transcript(cwd=str(project), session_id="aaaaaaaa-1111-2222-3333")
        == older / "messages.jsonl"
    )
    assert harness.session_id(newer / "messages.jsonl") == "bbbbbbbb-1111-2222-3333"


def test_vibe_ignores_directories_without_a_transcript(tmp_path):
    root = tmp_path / "session"
    project = tmp_path / "work"
    project.mkdir()
    d = root / "session_20260101_000000_aaaaaaaa"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(
        json.dumps({"environment": {"working_directory": str(project)}})
    )
    assert VibeHarness(root=root).find_transcript(cwd=str(project)) is None


def test_vibe_reports_the_sub_agents_it_spawned_itself():
    children = VibeHarness().native_children(FIXTURES / "vibe_messages.jsonl")
    # The fixture transcript's own directory has no meta.json next to it.
    assert children == []


def test_vibe_native_children_come_from_meta_json(tmp_path):
    session = tmp_path / "session_20260101_000000_aaaaaaaa"
    session.mkdir()
    (session / "messages.jsonl").write_text("")
    (session / "meta.json").write_text((FIXTURES / "vibe_meta.json").read_text())

    children = VibeHarness().native_children(session / "messages.jsonl")
    assert len(children) == 1
    assert children[0].agent == "explore"
    assert children[0].session_id == "9edd3dbf-b456-76cf-2f16-ea386d9c5cf2"
    assert children[0].relative_path.startswith("agents/explore_")


# ---- shared ------------------------------------------------------------


@pytest.mark.parametrize("harness", [ClaudeCodeHarness(), VibeHarness()])
@pytest.mark.parametrize("line", ["", "   ", "not json", "[]", "null", '{"role": 3}'])
def test_unparseable_lines_yield_nothing_rather_than_raising(harness, line):
    """A transcript being appended to as we read it is normal, not an error."""
    assert harness.parse(line, 0) == []


def test_long_text_is_clipped_before_it_reaches_the_bus():
    body = "x" * (MAX_TEXT * 3)
    events = VibeHarness().parse(json.dumps({"role": "user", "content": body}), 0)
    text = events[0].text
    assert len(text) < len(body)
    assert text.startswith("x" * 100)
    assert text.endswith(f"(+{MAX_TEXT * 2} chars)")


def test_status_is_only_ever_idle_or_working():
    """AWAITING_INPUT is not derivable from a transcript. See harness/base.py."""
    events = events_for(ClaudeCodeHarness(), FIXTURES / "claude_code.jsonl")
    assert {status_after(e) for e in events} <= {Status.IDLE, Status.WORKING}
    assert status_after(events[-1]) is Status.IDLE
    assert status_after(events[0]) is Status.WORKING
