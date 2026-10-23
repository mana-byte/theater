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
_SIDE = claude_record(
    "side", "u0", message={"id": "sm", "stop_reason": "end_turn", "content": []}, isSidechain=True
)
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
    # A main-thread marker whose parent is an unread sidechain assistant must never seal it.
    "sidechain_parent_drops": ([_USER, claude_marker("mk", "side"), _SIDE], []),
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


def test_unsupported_versions_and_sidechains_never_seal():
    thinking = {"type": "thinking", "thinking": "hm"}
    text = {"type": "text", "text": "answer"}
    old = ClaudeCodeObserver()
    lines = [
        json.dumps({"type": "assistant", "message": {"stop_reason": "end_turn", "content": [b]}})
        for b in (thinking, text)
    ]
    # No marker contract below the verified floor: only the text-bearing fragment can end a turn.
    assert [e.text for e in _boundaries(old, lines)] == ["answer"]

    side = [
        claude_record(uuid, parent, isSidechain=True, message=body)
        for uuid, parent, body in (
            ("s1", None, {"id": "m", "stop_reason": "end_turn", "content": [text]}),
        )
    ]
    side.append(claude_record("s2", "s1", "system", subtype="turn_duration", isSidechain=True))
    assert _boundaries(ClaudeCodeObserver(), side) == []
