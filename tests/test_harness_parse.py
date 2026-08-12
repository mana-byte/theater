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
from shipped import ClaudeCodeHarness, CodexHarness, VibeHarness
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


# ---- codex -------------------------------------------------------------


def test_codex_turn_parses_to_the_expected_event_sequence():
    events = events_for(CodexHarness(), FIXTURES / "codex.jsonl")

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


def test_codex_raw_index_tracks_the_source_record():
    events = events_for(CodexHarness(), FIXTURES / "codex.jsonl")
    assert [e.raw_index for e in events] == [2, 3, 4, 5, 7, 10]


def test_codex_records_carry_their_own_timestamp():
    events = events_for(CodexHarness(), FIXTURES / "codex.jsonl")
    assert all(e.ts is not None for e in events)
    # ISO-8601 with a Z suffix, parsed as UTC rather than local time.
    assert events[0].ts == pytest.approx(1786534852.2, abs=0.01)


def test_codex_takes_the_reply_from_the_turn_boundary_not_the_final_message():
    """`task_complete` repeats the final agent_message, so only one may speak.

    It has to be the boundary record: the observer hands the turn-ending
    event's text back to whoever awaited the job.
    """
    events = events_for(CodexHarness(), FIXTURES / "codex.jsonl")
    final = [e for e in events if e.text == "<final answer 208 chars>"]
    assert len(final) == 1
    assert final[0].turn_end is True


def test_codex_commentary_is_kept_but_does_not_end_the_turn():
    events = events_for(CodexHarness(), FIXTURES / "codex.jsonl")
    assert events[1].text == "<commentary 112 chars>"
    assert status_after(events[1]) is Status.WORKING


def test_codex_tool_output_blocks_are_flattened():
    events = events_for(CodexHarness(), FIXTURES / "codex.jsonl")
    assert events[3].text == "Script completed\n<tool output 64 chars>"
    # The record carries only a call_id, and parse holds no state across lines.
    assert events[3].tool_name is None


def test_codex_mcp_calls_are_the_only_sight_of_theaters_own_tools():
    """They never appear as response_items, so this branch is load-bearing."""
    events = events_for(CodexHarness(), FIXTURES / "codex.jsonl")
    assert events[4].tool_name == "theater.list_participants"
    assert events[4].text == "<mcp result 41 chars>"


def test_codex_an_aborted_turn_ends_the_turn():
    """Otherwise a caller awaits a reply that a human has already cancelled."""
    record = {
        "timestamp": "2026-08-12T11:37:50.379Z",
        "type": "event_msg",
        "payload": {"type": "turn_aborted", "reason": "interrupted"},
    }
    events = CodexHarness().parse(json.dumps(record), 0)
    assert len(events) == 1
    assert events[0].kind is EventKind.ERROR
    assert events[0].text == "turn aborted: interrupted"
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
    harness = CodexHarness()
    assert harness.parse(json.dumps(call), 0)[0].tool_name == "wait"
    assert harness.parse(json.dumps(output), 1)[0].text == "done"


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

    harness = CodexHarness(root=root)
    assert harness.find_transcript(cwd=str(project)) == target
    assert harness.find_transcript(cwd="/nowhere") is None
    # A known session id is an exact glob: no scan, no date guessing.
    assert harness.find_transcript(cwd="/nowhere", session_id=sid) == target


def test_codex_session_id_is_the_uuid_tail_of_the_filename(tmp_path):
    """The uuid keeps its own hyphens, so the timestamp is what anchors it."""
    sid = "019ff5c6-717c-7a70-9ec4-66dd1f4d173e"
    path = tmp_path / f"rollout-2026-08-12T13-40-50-{sid}.jsonl"
    assert CodexHarness().session_id(path) == sid
    assert CodexHarness().session_id(tmp_path / "not-a-rollout.jsonl") is None


def test_codex_after_excludes_transcripts_that_predate_the_participant(tmp_path):
    root = tmp_path / "sessions"
    day = root / "2026" / "08" / "12"
    day.mkdir(parents=True)
    project = tmp_path / "work"
    project.mkdir()
    path = day / "rollout-2026-08-12T13-40-50-abc.jsonl"
    path.write_text(
        json.dumps(
            {"type": "session_meta", "payload": {"cwd": str(project)}}
        )
        + "\n"
    )

    harness = CodexHarness(root=root)
    assert harness.find_transcript(cwd=str(project), after=0) == path
    # A floor in the future can never be satisfied by an existing file. The
    # filename's timestamp is local and is never what gets compared.
    assert harness.find_transcript(cwd=str(project), after=4e9) is None


def test_codex_has_no_native_children(tmp_path):
    """Codex has no sub-agent mechanism, so this is a statement, not a stub."""
    assert CodexHarness().native_children(tmp_path / "anything.jsonl") == []


# ---- shared ------------------------------------------------------------


@pytest.mark.parametrize(
    "harness", [ClaudeCodeHarness(), CodexHarness(), VibeHarness()]
)
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


# ---- clip_text ---------------------------------------------------------

LONG = "x" * (MAX_TEXT * 3)

#: One record per branch that carries text, so the parametrization fails if a
#: branch forgets to honour clip_text. That has happened: a Claude Code user
#: text block clipped unconditionally, which `read_transcript` then served as
#: the "full" text.
UNCLIPPED_CASES = [
    (VibeHarness(), {"role": "user", "content": LONG}),
    (VibeHarness(), {"role": "assistant", "content": LONG}),
    (VibeHarness(), {"role": "tool", "content": LONG, "name": "bash"}),
    (
        ClaudeCodeHarness(),
        {"type": "assistant", "message": {"content": [{"type": "text", "text": LONG}]}},
    ),
    (
        ClaudeCodeHarness(),
        {"type": "user", "message": {"content": LONG}},
    ),
    (
        ClaudeCodeHarness(),
        {
            "type": "user",
            "message": {"content": [{"type": "text", "text": LONG}]},
        },
    ),
    (
        ClaudeCodeHarness(),
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": LONG}]},
        },
    ),
    (
        ClaudeCodeHarness(),
        {"type": "system", "level": "error", "error": LONG},
    ),
    (
        CodexHarness(),
        {"type": "event_msg", "payload": {"type": "user_message", "message": LONG}},
    ),
    (
        CodexHarness(),
        {
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": LONG,
                "phase": "commentary",
            },
        },
    ),
    (
        CodexHarness(),
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": LONG},
        },
    ),
    (
        CodexHarness(),
        {
            "type": "event_msg",
            "payload": {
                "type": "mcp_tool_call_end",
                "invocation": {"server": "theater", "tool": "send"},
                "result": {"Ok": {"content": [{"type": "text", "text": LONG}]}},
            },
        },
    ),
    (
        CodexHarness(),
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "output": [{"type": "input_text", "text": LONG}],
            },
        },
    ),
]


@pytest.mark.parametrize("harness,record", UNCLIPPED_CASES)
def test_clip_text_false_returns_the_whole_text(harness, record):
    """read_transcript asks for the record as written, on every branch."""
    events = harness.parse(json.dumps(record), 0, clip_text=False)
    assert events, "expected the record to parse into at least one event"
    assert any(e.text == LONG for e in events)


@pytest.mark.parametrize("harness,record", UNCLIPPED_CASES)
def test_the_same_branches_clip_by_default(harness, record):
    events = harness.parse(json.dumps(record), 0)
    assert events
    assert all(len(e.text) <= MAX_TEXT + 40 for e in events)


# ---- idle screens ------------------------------------------------------


@pytest.mark.parametrize(
    "harness", [ClaudeCodeHarness(), CodexHarness(), VibeHarness()]
)
@pytest.mark.parametrize("capture", ["", "\n", "   \n  \n"])
def test_a_blank_pane_is_not_a_prompt(harness, capture):
    """An empty capture means the pane has not drawn, not that it is waiting."""
    assert harness.is_idle_screen(capture) is False


@pytest.mark.parametrize(
    "harness,capture",
    [
        (VibeHarness(), "some output\n❯"),
        (VibeHarness(), "some output\n❯ "),
        (VibeHarness(), "some output\n❯\n\n"),
        (ClaudeCodeHarness(), "some output\n>"),
        (ClaudeCodeHarness(), "some output\n> "),
    ],
)
def test_a_bare_prompt_on_the_last_line_is_idle(harness, capture):
    assert harness.is_idle_screen(capture) is True


@pytest.mark.parametrize(
    "harness,capture",
    [
        (VibeHarness(), "❯ what model are you"),
        (VibeHarness(), "❯\nstill rendering output"),
        (ClaudeCodeHarness(), "> what model are you"),
        (ClaudeCodeHarness(), "> \nstill rendering output"),
    ],
)
def test_text_after_the_prompt_is_not_idle(harness, capture):
    """Someone typing is presence, and output still landing is work."""
    assert harness.is_idle_screen(capture) is False


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
    assert CodexHarness().is_idle_screen(CODEX_IDLE) is True


def test_codex_is_not_idle_while_a_turn_is_running():
    """The composer is still on screen mid-turn; the status line is the tell."""
    assert CodexHarness().is_idle_screen(CODEX_WORKING) is False
    assert CodexHarness().is_idle_screen(CODEX_IDLE + "\n" + CODEX_WORKING) is False


def test_codex_output_scrolled_past_the_composer_is_not_idle():
    """Beyond the tail window the pane is showing something else entirely."""
    trailing = "\n".join(["more output"] * 8)
    assert CodexHarness().is_idle_screen(CODEX_IDLE + "\n" + trailing) is False
