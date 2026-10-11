"""Claude's final answer is sealed by the causal ``turn_duration`` marker, never by a fragment."""

from __future__ import annotations

import json
from pathlib import Path

from theater.daemon.jobs import JobManager
from theater.daemon.observer import Observer, QuietClock, TurnAccumulator
from theater.harness.builtin.plugins.claude.observer import ClaudeCodeObserver
from theater.harness.source import TranscriptSource
from theater.models import JobState
from theater.provenance import TranscriptProvenance

SESSION = "11111111-1111-4111-8111-111111111111"
VERSION = "2.1.284"


def _record(uuid: str, parent: str | None, kind: str = "assistant", **extra) -> str:
    return json.dumps(
        {"type": kind, "uuid": uuid, "parentUuid": parent, "version": VERSION, **extra}
    )


def _fragment(uuid: str, parent: str | None, block: dict, message: str = "msg_1") -> str:
    message_body = {"id": message, "stop_reason": "end_turn", "content": [block]}
    return _record(uuid, parent, message=message_body)


def _marker(uuid: str, parent: str) -> str:
    return _record(uuid, parent, "system", subtype="turn_duration")


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
    _append(path, _record("u0", None, "user", message={"role": "user", "content": "go"}))
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
        observer._apply(participant.id, await source.read(), clock, turns)

    _append(path, _fragment("u1", "u0", {"type": "thinking", "thinking": "hm"}))
    await read_and_apply()
    assert jobs.get("h1").state == JobState.RUNNING

    _append(path, _fragment("u2", "u1", {"type": "text", "text": '{"ok": true}'}))
    await read_and_apply()
    assert jobs.get("h1").state == JobState.RUNNING

    # The marker reaches its final record through one attachment hop, in a later read.
    _append(path, _record("u3", "u2", "attachment"), _marker("u4", "u3"))
    await read_and_apply()
    job = jobs.get("h1")
    assert (job.state, job.result) == (JobState.DONE, '{"ok": true}')
    assert (job.structured_status, job.structured_result) == ("parsed", '{"ok": true}')


def test_a_proven_empty_final_is_one_empty_boundary_and_an_unproven_one_is_none():
    thinking = _fragment("u1", "u0", {"type": "thinking", "thinking": "hm"})
    marker = _marker("u2", "u1")

    (end,) = _boundaries(ClaudeCodeObserver(), [_record("u0", None, "user"), thinking, marker])
    assert (end.text, end.turn_id) == ("", "msg_1")

    # A restarted observer never saw the final record: fail closed rather than guess.
    assert _boundaries(ClaudeCodeObserver(), [marker]) == []
    # Attached mid-message: an earlier fragment may exist unseen, so the text is not provably whole.
    text = _fragment("u5", "gone", {"type": "text", "text": "tail"})
    assert _boundaries(ClaudeCodeObserver(), [text, _marker("u6", "u5")]) == []


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
        _record(uuid, parent, isSidechain=True, message=body)
        for uuid, parent, body in (
            ("s1", None, {"id": "m", "stop_reason": "end_turn", "content": [text]}),
        )
    ]
    side.append(_record("s2", "s1", "system", subtype="turn_duration", isSidechain=True))
    assert _boundaries(ClaudeCodeObserver(), side) == []
