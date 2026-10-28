"""Claude's final answer is sealed by the causal ``turn_duration`` marker, never by a fragment."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.claude_seal import claude_fragment, claude_marker, claude_record
from tests.support.observation import apply_batch
from theater.daemon.jobs import JobManager
from theater.daemon.observer import Observer, QuietClock, TurnAccumulator
from theater.harness.builtin.plugins.claude.observer import ClaudeCodeObserver
from theater.harness.normalization.timing import iso_epoch
from theater.harness.source import TranscriptSource
from theater.models import JobState
from theater.provenance import TranscriptProvenance

SESSION = "11111111-1111-4111-8111-111111111111"


def _boundaries(observer: ClaudeCodeObserver, lines: list[str]) -> list:
    events = [e for i, line in enumerate(lines) for e in observer.parse(line, i)]
    return [e for e in events if e.turn_end]


def _append(path: Path, *lines: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write("".join(line + "\n" for line in lines))


async def test_job_finishes_on_the_marker_with_the_final_text_split_across_reads(
    registry, tmp_path
):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    path = tmp_path / "claude" / "projects" / "project" / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True)
    _append(path, claude_record("u0", None, "user", message={"role": "user", "content": "go"}))
    source = TranscriptSource(
        ClaudeCodeObserver(root=tmp_path / "claude" / "projects"),
        cwd=str(cwd),
        session_id=SESSION,
        session_provenance=TranscriptProvenance.EXACT,
    )
    await source.read()
    source.commit_attachment()
    participant = registry.create_spawned(harness="claude", cwd=str(cwd), pid="p-claude")
    jobs = JobManager(registry.store)
    jobs.create(
        handle="h1",
        caller_id="c",
        target_id=participant.id,
        kind="send",
        response_format='{"type":"object"}',
    )
    observer = Observer(registry, harnesses={}, jobs=jobs)
    clock = QuietClock(quiet_since=1.0, screen_quiet_since=2.0, rescue_since=3.0)
    turns = TurnAccumulator()

    async def read_and_apply() -> None:
        apply_batch(observer, participant.id, await source.read(), clock, turns, source=source)

    _append(path, claude_fragment("u1", "u0", {"type": "thinking", "thinking": "hm"}))
    await read_and_apply()
    assert jobs.get("h1").state == JobState.RUNNING

    _append(path, claude_fragment("u2", "u1", {"type": "text", "text": '{"ok": true}'}))
    await read_and_apply()
    assert jobs.get("h1").state == JobState.RUNNING

    # The marker reaches its final record through one attachment hop, in a later read.
    _append(path, claude_record("u3", "u2", "attachment"), claude_marker("u4", "u3"))
    await read_and_apply()
    job = jobs.get("h1")
    assert (job.state, job.result) == (JobState.DONE, '{"ok": true}')
    assert (job.structured_status, job.structured_result) == ("parsed", '{"ok": true}')


def test_a_proven_empty_final_is_one_empty_boundary_and_an_unproven_one_is_none():
    thinking = claude_fragment("u1", "u0", {"type": "thinking", "thinking": "hm"})
    marker = claude_marker("u2", "u1")

    (end,) = _boundaries(
        ClaudeCodeObserver(), [claude_record("u0", None, "user"), thinking, marker]
    )
    assert (end.text, end.turn_id) == ("", "msg_1")

    # A restarted observer never saw the final record: fail closed rather than guess.
    assert _boundaries(ClaudeCodeObserver(), [marker]) == []
    # Attached mid-message: an earlier fragment may exist unseen, so the text is not provably whole.
    text = claude_fragment("u5", "gone", {"type": "text", "text": "tail"})
    assert _boundaries(ClaudeCodeObserver(), [text, claude_marker("u6", "u5")]) == []


def _boundaries_at(observer: ClaudeCodeObserver, indexed: list[tuple[str, int]]) -> list:
    events = [e for line, i in indexed for e in observer.parse(line, i)]
    return [e for e in events if e.turn_end]


# Shared out-of-order shape: a1's parentUuid ("late") is an attachment flushed after the marker.
_USER = claude_record("u0", None, "user")
_ANSWER = claude_fragment("a1", "late", {"type": "text", "text": "done"})
_ATTACH = claude_record("att1", "a1", "attachment")
_MARKER = claude_marker("mk", "att1")
_LATE = claude_record("late", "u0", "attachment")
_SEAL_CASES = {
    # The intended fix: hold the marker, seal once its late parent arrives.
    "late_attachment_seals_once": ([_USER, _ANSWER, _ATTACH, _MARKER, _LATE], ["done"]),
    "parent_never_arrives": ([_USER, _ANSWER, _ATTACH, _MARKER], []),
    "new_user_drops": (
        [_USER, _ANSWER, _ATTACH, _MARKER, claude_record("u9", "att1", "user"), _LATE],
        [],
    ),
    # A newer resolvable marker supersedes the held one; only the new answer seals.
    "newer_marker_supersedes": (
        [
            _USER,
            _ANSWER,
            _ATTACH,
            _MARKER,
            claude_fragment("a2", "u0", {"type": "text", "text": "new"}, "msg_2"),
            claude_marker("mk2", "a2"),
            _LATE,
        ],
        ["new"],
    ),
    # A late record that could itself be a fragment (reversed assembly) must drop, not seal.
    "late_assistant_fragment_drops": (
        [
            _USER,
            claude_fragment("tail", "head", {"type": "text", "text": "SECOND"}),
            claude_marker("mk", "tail"),
            claude_fragment("head", "u0", {"type": "text", "text": "FIRST"}),
        ],
        [],
    ),
    # A late record that carries its own terminal event (a system error) drops the held marker
    # and emits only its own boundary — never two (its error plus the stale answer).
    "late_system_error_drops": (
        [
            _USER,
            _ANSWER,
            _ATTACH,
            _MARKER,
            claude_record("late", "u0", "system", level="error", error="boom"),
        ],
        ["boom"],
    ),
    # No sealed-id dedup: a message id reused across turns seals each turn, exactly like base.
    "reused_message_id_seals_each_turn": (
        [
            claude_record("u0", None, "user"),
            claude_fragment("a0", "u0", {"type": "text", "text": "FIRST"}, "shared"),
            claude_marker("m0", "a0"),
            claude_record("u1", None, "user"),
            claude_fragment("a1b", "u1", {"type": "text", "text": "SECOND"}, "shared"),
            claude_marker("m1", "a1b"),
        ],
        ["FIRST", "FIRST\n\nSECOND"],
    ),
}


@pytest.mark.parametrize("case", list(_SEAL_CASES))
def test_held_marker_seals_only_the_genuine_late_attachment(case: str) -> None:
    lines, expected = _SEAL_CASES[case]
    assert [e.text for e in _boundaries(ClaudeCodeObserver(), lines)] == expected


def test_a_held_marker_drops_on_index_regression_and_session_change() -> None:
    base = [(_USER, 0), (_ANSWER, 1), (_ATTACH, 2), (_MARKER, 3)]
    # Rewind/replacement re-reads from index 0: the held marker must not seal the old answer.
    assert _boundaries_at(ClaudeCodeObserver(), [*base, (_LATE, 0)]) == []
    # A later record from a different session must not complete the held marker.
    marker_a = claude_record("mk", "att1", "system", subtype="turn_duration", sessionId="sid-A")
    late_b = claude_record("late", "u0", "attachment", sessionId="sid-B")
    other = [(_USER, 0), (_ANSWER, 1), (_ATTACH, 2), (marker_a, 3), (late_b, 4)]
    assert _boundaries_at(ClaudeCodeObserver(), other) == []


def test_a_resolving_record_keeps_its_own_baseline_events() -> None:
    # A late fragment-capable record drops the held marker (fail closed) yet still projects its
    # own events: suppression touches only a synthesized seal boundary, never the record's own.
    observer = ClaudeCodeObserver()
    lines = [_USER, _ANSWER, _ATTACH, _MARKER]
    for i, line in enumerate(lines):
        observer.parse_record(line, i)
    tool = claude_record(
        "late",
        "u0",
        cwd="/repo",
        message={
            "id": "tool-msg",
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Read",
                    "input": {"file_path": "/repo/a.txt"},
                }
            ],
        },
    )
    parsed = observer.parse_record(tool, len(lines))
    assert [e.turn_end for e in parsed.events] == [False]  # no seal boundary, only the tool call
    assert parsed.trajectory_events is None  # the record's own events are not suppressed


def test_a_thinking_only_fragment_never_ends_the_turn_the_marker_seals_the_text():
    thinking = {"type": "thinking", "thinking": "hm"}
    text = {"type": "text", "text": "answer"}
    old = ClaudeCodeObserver()
    lines = [
        claude_record("u0", None, "user", message={"role": "user", "content": "go"}),
        claude_fragment("t1", "u0", thinking, "msg_think"),
        claude_fragment("t2", "t1", text, "msg_text"),
        claude_marker("m", "t2"),
    ]
    # A thinking-only fragment never ends a turn; only the marker names the answer that does.
    assert [e.text for e in _boundaries(old, lines)] == ["answer"]


# The session was launched at second 3: what is stamped earlier is history copied from a fork.
LAUNCHED = iso_epoch("2026-01-01T00:00:03Z")


def _stamped(line: str, second: int, **extra) -> str:
    record = json.loads(line)
    record.update(timestamp=f"2026-01-01T00:00:{second:02d}Z", **extra)
    return json.dumps(record)


def _answer(uuid: str, parent: str | None, text: str, message: str, second: int) -> str:
    return _stamped(claude_fragment(uuid, parent, {"type": "text", "text": text}, message), second)


def _prompt(uuid: str, parent: str | None, text: str, second: int, **extra) -> str:
    return _stamped(claude_record(uuid, parent, "user", message={"content": text}), second, **extra)


def _history() -> list[str]:
    """What Claude copies into a fork after the fork's first turn has been written."""
    return [
        _answer("old_answer", "old_user", "OLD", "msg_old", 1),
        _stamped(claude_marker("old_marker", "old_answer"), 2),
    ]


def _steps(lines: list[str], *, launched_at: float | None = LAUNCHED) -> list[list]:
    """The events each record yields, read in order by one observer."""
    observer = ClaudeCodeObserver(launched_at=launched_at)
    return [observer.parse(line, i) for i, line in enumerate(lines)]


def _ended(lines: list[str], **kwargs) -> list[str]:
    return [e.text for events in _steps(lines, **kwargs) for e in events if e.turn_end]


def _answered(lines: list[str], **kwargs) -> bool:
    """Whether the new turn's message ended a turn, whatever text was joined to it."""
    return any(
        e.turn_id == "msg_new" for events in _steps(lines, **kwargs) for e in events if e.turn_end
    )


# The new turn's answer and marker land first; the prompt they answer lands last.
_OLD_USER = _prompt("old_user", None, "old", 1)
_NEW_ANSWER = _answer("new_answer", "new_user", "NEW", "msg_new", 5)
_NEW_MARKER = _stamped(claude_marker("new_marker", "new_answer"), 6)
_NEW_PROMPT = _prompt("new_user", "old_marker", "new", 4)
_FORK = [_OLD_USER, _NEW_ANSWER, _NEW_MARKER, *_history(), _NEW_PROMPT]


def test_a_held_marker_survives_the_history_copied_after_it_and_seals_on_its_late_prompt():
    events = [e for step in _steps(_FORK) for e in step]

    # The copied history's own end answers no job; the prompt is heard before its answer ends.
    assert [e.text for e in events if e.turn_end] == ["NEW"]
    last_user = max(i for i, e in enumerate(events) if e.kind.value == "user")
    assert events[last_user].text == "new" and events[last_user + 1].text == "NEW"


def test_the_late_prompt_keeps_its_own_trajectory_event_but_not_the_synthesized_boundary():
    observer = ClaudeCodeObserver(launched_at=LAUNCHED)
    parsed = [observer.parse_record(line, i) for i, line in enumerate(_FORK)]

    assert [e.turn_end for e in parsed[-1].events] == [False, True]
    assert [e.kind.value for e in parsed[-1].trajectory_events] == ["user"]
    assert parsed[-2].events == ()  # the copied history's marker seals nothing


def test_nothing_is_history_without_a_launch_time():
    # Adopted participants have none: every record could be live, so the held marker drops.
    assert "NEW" not in _ended(_FORK, launched_at=None)


@pytest.mark.parametrize(
    "intruder",
    [
        pytest.param(lambda: _prompt("next", "new_marker", "next", 7), id="newer-prompt"),
        pytest.param(
            lambda: _answer("next", "new_marker", "MORE", "msg_next", 7), id="newer-reply"
        ),
        pytest.param(lambda: _prompt("other", "old_user", "other", 4), id="prompt-of-another-turn"),
        pytest.param(lambda: claude_record("untimed", "old_user", "user"), id="untimed"),
        pytest.param(  # a later fragment of the held message
            lambda: _answer("frag", "old_user", "FIRST", "msg_new", 4), id="same-message-fragment"
        ),
        pytest.param(
            lambda: _stamped(claude_record("err", "old_user", "system", level="error"), 4),
            id="terminal-error",
        ),
    ],
)
def test_a_launch_local_record_that_is_not_the_awaited_prompt_still_drops_the_held_marker(intruder):
    lines = [_OLD_USER, _NEW_ANSWER, _NEW_MARKER, intruder(), *_history(), _NEW_PROMPT]

    assert not _answered(lines)


@pytest.mark.parametrize("extra", [{"isCompactSummary": True}, {"isMeta": True}])
def test_a_summary_or_injected_text_is_not_the_prompt_a_turn_began_with(extra):
    # The held answer's parent is this user record, yet nobody typed it: nothing says whose turn.
    answer = _answer("new_answer", "new_user", "NEW", "msg_new", 5)
    lines = [
        _OLD_USER,
        answer,
        _NEW_MARKER,
        *_history(),
        _prompt("new_user", "old_marker", "x", 4, **extra),
    ]

    assert "NEW" not in _ended(lines)


@pytest.mark.parametrize("held_by", ["tool-records-written-late", "bare-attachment-after"])
def test_a_turn_whose_tool_records_were_written_late_is_never_released_without_its_prompt(held_by):
    tool_call = _stamped(
        claude_record(
            "new_call",
            "new_user",
            message={"id": "msg_call", "stop_reason": "tool_use", "content": []},
        ),
        4,
    )
    tool_result = _stamped(
        claude_record(
            "new_result",
            "new_call",
            "user",
            message={"content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]},
        ),
        4,
    )
    answer = _answer("new_answer", "new_result", "NEW", "msg_new", 5)
    lines = [_OLD_USER, answer, _NEW_MARKER, *_history(), tool_result, tool_call]
    if held_by == "bare-attachment-after":
        lines.append(_stamped(claude_record("late", "new_marker", "attachment"), 7))
    lines.append(_NEW_PROMPT)

    assert "NEW" not in _ended(lines)


def test_history_that_fills_a_link_of_the_held_chain_drops_it_instead_of_completing_it():
    # The turn would continue from a copied record: there is no prompt of its own to be heard.
    link = _answer("late", "old_marker", "TOOL", "msg_call", 1)
    answer = _answer("new_answer", "late", "NEW", "msg_new", 5)
    lines = [
        _OLD_USER,
        *_history(),
        answer,
        _NEW_MARKER,
        link,
        claude_record("a", "late", "attachment"),
    ]

    assert "NEW" not in _ended(lines)


def test_a_marker_written_by_a_live_turn_still_supersedes_the_held_one():
    # Two queued turns written out of order: the earlier one is not history, it is a live turn.
    user_a, answer_a = _prompt("user_a", None, "A", 4), _answer("ans_a", "user_a", "A!", "m_a", 5)
    answer_b = _answer("ans_b", "user_b", "B!", "m_b", 8)
    marker_b = _stamped(claude_marker("mark_b", "ans_b"), 9)
    marker_a = _stamped(claude_marker("mark_a", "ans_a"), 6)
    user_b = _prompt("user_b", "mark_a", "B", 7)

    assert _ended([user_a, answer_a, answer_b, marker_b, marker_a, user_b]) == ["A!"]


def test_a_copied_marker_from_another_read_does_not_keep_the_held_one():
    session = {"sessionId": "A"}
    held = [
        _stamped(_OLD_USER, 1, **session),
        _stamped(_history()[0], 1, **session),
        _stamped(_NEW_ANSWER, 5, **session),
        _stamped(_NEW_MARKER, 6, **session),
    ]
    prompt = _stamped(_NEW_PROMPT, 4, **session)

    def sealed(*lines: tuple[str, int]) -> bool:
        observer = ClaudeCodeObserver(launched_at=LAUNCHED)
        events = [e for line, i in lines for e in observer.parse(line, i)]
        return any(e.turn_end and e.turn_id == "msg_new" for e in events)

    indexed = [(line, i) for i, line in enumerate(held)]
    after = len(held)
    copied = _stamped(claude_marker("old_marker", "old_answer"), 2, **session)
    # Copied history of the same read keeps the held marker; a marker of another session, or at
    # an index the stream has already passed, is a discontinuity that clears it.
    assert sealed(*indexed, (copied, after), (prompt, after + 1))
    other = _stamped(copied, 2, sessionId="B")
    assert not sealed(*indexed, (other, after), (prompt, after + 1))
    assert not sealed(*indexed, (copied, 1), (prompt, after + 1))


def test_a_marker_whose_final_message_names_no_id_cannot_borrow_the_previous_turns():
    # Without a message id the marker's chain walks through the late prompt into the turn before.
    answer = _stamped(
        claude_record(
            "new_answer",
            "new_user",
            message={"stop_reason": "end_turn", "content": [{"type": "text", "text": "NEW"}]},
        ),
        5,
    )
    steps = _steps([_OLD_USER, *_history(), answer, _NEW_MARKER, _NEW_PROMPT])

    assert not any(e.turn_end for e in steps[-1])


def test_metadata_and_attachments_never_release_a_turn_that_waits_for_its_prompt():
    attachment = _stamped(claude_record("late", "new_marker", "attachment"), 7)
    copied = _stamped(claude_record("copy", "old_marker", "attachment"), 2)

    assert not _answered([_OLD_USER, _NEW_ANSWER, _NEW_MARKER, copied, attachment, *_history()])


def test_history_hiding_behind_a_missing_record_cannot_complete_the_held_chain_later():
    # The held answer's parent arrives after the marker, as an attachment on a copied record.
    answer = _answer("new_answer", "late", "NEW", "msg_new", 5)
    ancestor = _answer("ancestor", "old_marker", "TOOL", "msg_call", 1)
    late = _stamped(claude_record("late", "ancestor", "attachment"), 7)

    assert not _answered([_OLD_USER, *_history(), answer, _NEW_MARKER, ancestor, late])


def test_copied_history_cannot_add_to_the_held_message():
    # A record of the copy that reuses the held message's id must not lend it its text.
    reused = _answer("reused", "old_user", "OLD", "msg_new", 1)

    assert not _answered([_OLD_USER, _NEW_ANSWER, _NEW_MARKER, reused, _NEW_PROMPT])


def test_a_stamp_before_the_launch_is_no_history_when_the_record_descends_from_live_ones():
    # Queued turns A and B written out of order, A's marker carrying a stamp from before the launch
    # (a clock that stepped back): it descends from A's own answer, so it is a live turn's end.
    user_a, answer_a = _prompt("user_a", None, "A", 4), _answer("ans_a", "user_a", "A!", "m_a", 5)
    answer_b = _answer("ans_b", "user_b", "B!", "m_b", 8)
    marker_b = _stamped(claude_marker("mark_b", "ans_b"), 9)
    stepped_back = _stamped(claude_marker("mark_a", "ans_a"), 1)
    user_b = _prompt("user_b", "mark_a", "B", 7)

    assert _ended([user_a, answer_a, answer_b, marker_b, stepped_back, user_b]) == ["A!"]


def test_a_prompt_stamped_before_the_launch_that_follows_the_held_marker_drops_it():
    follower = _prompt("follower", "new_marker", "next", 1)

    assert not _answered([_OLD_USER, _NEW_ANSWER, _NEW_MARKER, follower, _NEW_PROMPT])


@pytest.mark.parametrize(
    "prompt",
    [
        pytest.param(lambda: _prompt("new_user", "old_marker", "  ", 4), id="blank"),
        pytest.param(lambda: claude_record("new_user", "old_marker", "user"), id="untimed-no-text"),
        pytest.param(  # nothing says it was written by this session
            lambda: claude_record("new_user", "old_marker", "user", message={"content": "new"}),
            id="untimed",
        ),
        pytest.param(  # nor that it was written after the launch
            lambda: _prompt("new_user", "old_marker", "new", 2), id="stamped-before-the-launch"
        ),
    ],
)
def test_only_a_typed_main_thread_prompt_with_a_launch_stamp_releases_the_turn(prompt):
    assert not _answered([_OLD_USER, _NEW_ANSWER, _NEW_MARKER, *_history(), prompt()])


def test_metadata_written_while_a_turn_waits_for_its_prompt_does_not_disturb_it():
    attachment = _stamped(claude_record("att", "new_marker", "attachment"), 6)
    metadata = json.dumps({"type": "api-request", "sessionId": "x"})
    lines = [_OLD_USER, _NEW_ANSWER, _NEW_MARKER, attachment, metadata, *_history(), _NEW_PROMPT]

    assert _ended(lines) == ["NEW"]


def test_a_fresh_sessions_late_attachment_still_seals_the_first_turn():
    # Claude writes an answer and its marker before the attachment that is the answer's parent:
    # with a launch time known, nothing of that is history, and the base release applies.
    user = _prompt("user", None, "go", 4)
    answer = _answer("answer", "late", "done", "msg_first", 5)
    attachment = _stamped(claude_record("att", "answer", "attachment"), 5)
    marker = _stamped(claude_marker("mark", "att"), 6)
    late = _stamped(claude_record("late", "user", "attachment"), 6)
    metadata = json.dumps({"type": "api-request"})

    assert _ended([user, answer, attachment, marker, metadata, late]) == ["done"]


def test_a_forks_first_turn_whose_linking_attachment_was_written_before_its_prompt_is_released():
    # The same shape inside a fork: history was copied meanwhile, and the answer's parent is an
    # attachment of the prompt that is read before the prompt itself.
    answer = _answer("answer", "late", "NEW", "msg_new", 5)
    attachment = _stamped(claude_record("att", "answer", "attachment"), 5)
    marker = _stamped(claude_marker("mark", "att"), 6)
    late = _stamped(claude_record("late", "new_user", "attachment"), 6)
    lines = [_OLD_USER, answer, attachment, marker, *_history(), late, _NEW_PROMPT]

    assert _ended(lines) == ["NEW"]
    assert not _ended(lines[:-1])


# The new turn's answer descends from two attachments Claude wrote after the prompt, which itself
# lands after the answer: a resumed session whose tool servers connected late.
_LATE_ANSWER = _answer("new_answer", "att2", "NEW", "msg_new", 5)
_LATE_SNAPSHOT = _stamped(claude_record("snap", "new_answer", "attachment"), 5)
_LATE_MARKER = _stamped(claude_marker("new_marker", "snap"), 6)
_ATT1 = _stamped(claude_record("att1", "new_user", "attachment"), 6)
_ATT2 = _stamped(claude_record("att2", "att1", "attachment"), 6)


def _late(*after_prompt: str) -> list[str]:
    head = [_OLD_USER, _LATE_ANSWER, _LATE_SNAPSHOT, _LATE_MARKER, *_history(), _NEW_PROMPT]
    return [*head, *after_prompt]


def test_a_forks_first_turn_is_released_when_the_attachments_after_its_prompt_complete_the_chain():
    steps = _steps(_late(_ATT1, _ATT2))

    assert [e.turn_end for e in steps[-3]] == [False]  # the prompt is heard, its answer waits
    assert steps[-3][0].text == "new" and steps[-2] == []
    assert [(e.text, e.turn_end) for e in steps[-1]] == [("NEW", True)]


def test_an_unrelated_attachment_does_not_stop_a_turn_waiting_for_its_chain():
    stray = _stamped(claude_record("stray", "old_user", "attachment"), 6)

    assert _ended(_late(_ATT1, stray, _ATT2)) == ["NEW"]


@pytest.mark.parametrize(
    "link",
    [
        pytest.param(lambda: _stamped(claude_record("att1", "new_user", "system"), 6), id="system"),
        pytest.param(lambda: _prompt("att1", "new_user", "typed", 6), id="prompt"),
        pytest.param(  # a reply is a fragment of an answer, never a link to one
            lambda: _answer("att1", "new_user", "MORE", "msg_more", 6), id="reply"
        ),
        pytest.param(  # nothing says this session wrote it
            lambda: _stamped(claude_record("att1", "new_user", "attachment"), 1), id="before-launch"
        ),
        pytest.param(  # nothing leads it to the prompt
            lambda: _stamped(claude_record("att1", "old_marker", "attachment"), 6), id="elsewhere"
        ),
    ],
)
def test_nothing_but_this_sessions_own_attachments_may_link_an_answer_to_its_prompt(link):
    assert not _answered(_late(link(), _ATT2))


def test_a_tool_round_between_the_prompt_and_the_answer_is_not_an_attachment_chain():
    # Only attachments may link them: a tool call and its result read before the marker stay out of
    # what a prompt may release, as they always did.
    call = _answer("call", "new_user", "CALL", "msg_call", 5)
    result = _stamped(
        claude_record(
            "result",
            "call",
            "user",
            message={"content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]},
        ),
        5,
    )
    att2 = _stamped(claude_record("att2", "result", "attachment"), 6)
    head = [_OLD_USER, call, result, _LATE_ANSWER, _LATE_SNAPSHOT, _LATE_MARKER, *_history()]

    assert not _answered([*head, _NEW_PROMPT, att2])


@pytest.mark.parametrize("through_attachments", [False, True], ids=["prompt", "attachments"])
def test_an_answer_written_as_several_fragments_is_released_through_its_first_one(
    through_attachments,
):
    # A thinking record then a text record of one message: the text's parent is its own fragment.
    first_parent = "att2" if through_attachments else "new_user"
    thinking = _stamped(
        claude_fragment("think", first_parent, {"type": "thinking", "thinking": "hm"}, "msg_new"), 5
    )
    answer = _answer("new_answer", "think", "NEW", "msg_new", 5)
    head = [_OLD_USER, thinking, answer, _LATE_SNAPSHOT, _LATE_MARKER, *_history(), _NEW_PROMPT]

    assert _ended([*head, *([_ATT1, _ATT2] if through_attachments else [])]) == ["NEW"]


@pytest.mark.parametrize("identity", [None, "absent"], ids=["null", "absent"])
def test_a_prompt_without_an_identity_releases_nothing_and_breaks_nothing(identity):
    # A root attachment ends the answer's chain at no uuid: that must not equal a missing prompt id.
    answer = _answer("new_answer", "root", "NEW", "msg_new", 5)
    root = _stamped(claude_record("root", None, "attachment"), 5)
    record = json.loads(_prompt("new_user", "old_marker", "new", 4))
    record.pop("uuid") if identity == "absent" else record.update(uuid=None)
    lines = [_OLD_USER, answer, _LATE_SNAPSHOT, _LATE_MARKER, *_history(), root, json.dumps(record)]

    assert not _answered(lines)


def test_a_second_prompt_while_the_chain_is_unread_drops_the_turn():
    follower = _prompt("next", "new_marker", "next", 7)

    assert not _answered(_late(follower, _ATT1, _ATT2))


def test_a_chain_that_is_never_completed_releases_nothing():
    assert not _answered(_late(_ATT1))
    assert not _answered(_late())


def test_a_session_without_a_launch_time_never_releases_through_attachments():
    assert not _answered(_late(_ATT1, _ATT2), launched_at=None)


def test_a_chain_of_many_attachments_is_still_followed():
    parents = ["new_user", *(f"x{i}" for i in range(11))]
    chain = [
        _stamped(claude_record(f"x{i}", parent, "attachment"), 6)
        for i, parent in enumerate(parents)
    ]
    answer = _answer("new_answer", "x10", "NEW", "msg_new", 5)

    assert _answered(
        [_OLD_USER, answer, _LATE_SNAPSHOT, _LATE_MARKER, *_history(), _NEW_PROMPT, *chain]
    )


def test_a_prompt_stamped_before_the_launch_never_releases_even_without_copied_history():
    prompt = _prompt("new_user", "old_marker", "new", 2)

    assert not _answered([_OLD_USER, _NEW_ANSWER, _NEW_MARKER, prompt])


def test_a_held_marker_of_copied_history_is_dropped_by_the_history_that_follows_it():
    # An old turn's marker whose chain is incomplete is held like any other; nothing here says
    # it belongs to this session, so the next copied record drops it instead of passing it by.
    old_answer = _answer("old_answer", "old_gap", "OLD", "msg_old", 1)
    old_marker = _stamped(claude_marker("old_marker", "old_answer"), 2)
    other = _answer("other", "old_user", "OTHER", "msg_other", 1)
    gap = _stamped(claude_record("old_gap", "old_user", "attachment"), 1)

    assert _ended([_OLD_USER, old_answer, old_marker, other, gap]) == []


def test_a_chain_completed_through_copied_history_is_not_released_after_a_copied_marker():
    # Only a copied marker was skipped while holding, yet the answer's late parent leads into
    # history: the turn has no prompt of this session's, so it releases to no job.
    answer = _answer("new_answer", "late", "NEW", "msg_new", 5)
    marker = _stamped(claude_marker("new_marker", "new_answer"), 6)
    late = _stamped(claude_record("late", "old_marker", "attachment"), 7)
    lines = [_OLD_USER, *_history()[:1], answer, marker, _history()[1], late]

    assert not _answered(lines)


def _queued_turns(*, copied: bool) -> list[str]:
    """Turns A and B queued, B's answer and marker written before A's marker and B's prompt."""
    user_a, answer_a = _prompt("user_a", None, "A", 4), _answer("ans_a", "user_a", "A!", "m_a", 5)
    answer_b = _answer("ans_b", "user_b", "B!", "m_b", 8)
    marker_b = _stamped(claude_marker("mark_b", "ans_b"), 9)
    marker_a = _stamped(claude_marker("mark_a", "ans_a"), 6)
    user_b = _prompt("user_b", "mark_a", "B", 7)
    return [
        *([_OLD_USER] if copied else []),
        user_a,
        answer_a,
        answer_b,
        marker_b,
        user_b,
        marker_a,
    ]


@pytest.mark.parametrize("copied", [False, True], ids=["fresh", "fork"])
def test_a_later_turns_prompt_never_releases_it_ahead_of_an_earlier_turn(copied):
    # Only a session's first prompt is released by the late-prompt rule: with another turn
    # queued before it, the turn ends are left for the earlier turn's marker to match.
    assert _ended(_queued_turns(copied=copied)) == ["A!"]


def test_copied_history_is_withheld_from_live_events_but_kept_for_the_trajectory():
    copied = _prompt("copy", "old_marker", "an old prompt", 2)
    observer = ClaudeCodeObserver(launched_at=LAUNCHED)
    parsed = [
        observer.parse_record(line, i)
        for i, line in enumerate([_OLD_USER, *_history(), _NEW_ANSWER, _NEW_MARKER, copied])
    ]

    assert parsed[-1].events == ()  # not something the reducer should hear
    assert [e.text for e in parsed[-1].trajectory_events] == ["an old prompt"]
    assert [e.text for e in parsed[2].events] != []  # history read before the hold stays live


def test_a_stamp_before_the_launch_proves_nothing_while_its_ancestry_is_unread():
    # Turn A's marker is stamped from before the launch, but its parent has not been read: it is
    # not provably copied history, so it takes the base path and ends A, not B.
    user_a, answer_a = _prompt("user_a", None, "A", 4), _answer("ans_a", "user_a", "A!", "m_a", 5)
    answer_b = _answer("ans_b", "user_b", "B!", "m_b", 8)
    marker_b = _stamped(claude_marker("mark_b", "ans_b"), 9)
    marker_a = _stamped(claude_marker("mark_a", "late_a"), 1)
    late_a = _stamped(claude_record("late_a", "ans_a", "attachment"), 6)
    user_b = _prompt("user_b", "mark_a", "B", 7)

    assert _ended([user_a, answer_a, answer_b, marker_b, marker_a, late_a, user_b]) == ["A!"]


def test_a_prompt_after_the_final_message_does_not_release_it():
    # The marker's parent is a later prompt of the same thread: that prompt is not what the
    # held message answers, and the message's own parent is copied history.
    answer = _answer("ans", "gap", "ANSWER_A", "msg_new", 5)
    later = _prompt("later", "ans", "Q", 6)
    marker = _stamped(claude_marker("mark", "later"), 7)
    copied = _answer("copy", "old_user", "COPY", "msg_copy", 1)
    gap = _stamped(claude_record("gap", "old_marker", "attachment"), 4)

    assert not _answered([_OLD_USER, *_history(), answer, later, marker, copied, gap])


def test_the_final_message_must_be_this_sessions_own_to_be_released_by_a_prompt():
    answer = _answer("ans", "new_user", "NEW", "msg_new", 1)  # stamped before the launch
    marker = _stamped(claude_marker("mark", "ans"), 6)

    assert not _answered([_OLD_USER, answer, marker, _NEW_PROMPT])


def test_a_session_that_never_showed_copied_history_has_only_the_base_rules():
    # No fork, no copy: a prompt written after the answer it began is a new user turn, which drops
    # the held marker exactly as before.
    prompt = _prompt("new_user", None, "new", 4)

    assert not _answered([_NEW_ANSWER, _NEW_MARKER, prompt])


def test_two_late_prompts_do_not_let_the_second_turn_end_before_the_first():
    # Prompts of two turns both written after their answers, the later turn's records first:
    # only the prompt that follows the copied history alone may release a turn, so it is A's.
    user_a, user_b = _prompt("ua", "old_marker", "A", 4), _prompt("ub", "ma", "B", 7)
    answer_a, answer_b = (
        _answer("aa", "ua", "ANSWER_A", "m_a", 5),
        _answer("ab", "ub", "ANS_B", "m_b", 8),
    )
    marker_a, marker_b = (
        _stamped(claude_marker("ma", "aa"), 6),
        _stamped(claude_marker("mb", "ab"), 9),
    )
    lines = [_OLD_USER, *_history(), answer_a, answer_b, marker_b, user_b, user_a, marker_a]

    assert "ANS_B" not in _ended(lines)


def test_a_prompt_whose_predecessor_was_never_read_is_not_a_forks_first_prompt():
    # A bounded attach need not contain the launch: an old stamp elsewhere proves no copy.
    old_attachment = _stamped(claude_record("stray", "unread", "attachment"), 1)
    prompt = _prompt("new_user", "unread_predecessor", "new", 4)

    assert not _answered([old_attachment, _NEW_ANSWER, _NEW_MARKER, prompt])


def test_a_prompt_cannot_lend_an_old_fragment_the_held_message_text():
    # A copied fragment seen before the hold shares the answer's message id: the text is not this
    # session's alone, so nothing here may release it.
    reused = _answer("reused", "old_user", "OLD", "msg_new", 1)

    assert not _answered([_OLD_USER, reused, *_history(), _NEW_ANSWER, _NEW_MARKER, _NEW_PROMPT])


def test_a_marker_stamped_before_the_launch_is_not_a_forks_first_turn():
    marker = _stamped(claude_marker("new_marker", "new_answer"), 1)

    assert not _answered([_OLD_USER, *_history(), _NEW_ANSWER, marker, _NEW_PROMPT])


def test_metadata_without_an_identity_is_never_copied_history():
    # A record with no uuid cannot be proven to be part of a copy, so it neither marks the hold
    # as having skipped history nor stops a late attachment from releasing the first turn.
    user = _prompt("user", None, "go", 4)
    answer = _answer("answer", "late", "done", "msg_first", 5)
    marker = _stamped(claude_marker("mark", "answer"), 6)
    stray = json.dumps({"type": "api-request", "timestamp": "2026-01-01T00:00:01Z"})
    late = _stamped(claude_record("late", "user", "attachment"), 6)

    assert _ended([user, answer, marker, stray, late]) == ["done"]


def test_a_final_message_stamped_before_the_launch_is_not_released_even_without_text():
    thinking = _stamped(
        claude_fragment("ans", "new_user", {"type": "thinking", "thinking": "hm"}, "msg_new"), 1
    )
    marker = _stamped(claude_marker("mark", "ans"), 6)

    assert not _answered([_OLD_USER, *_history(), thinking, marker, _NEW_PROMPT])
