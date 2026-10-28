"""Copied history never evicts a fork's answer or becomes a live turn."""

from __future__ import annotations

import json

import pytest

from tests.support.claude_seal import claude_fork, claude_fragment, claude_marker, claude_record
from tests.support.observation import apply_batch
from theater.daemon.jobs import JobManager
from theater.daemon.observer import Observer, QuietClock, TurnAccumulator
from theater.harness import EventKind
from theater.harness.builtin.plugins.claude.constants import (
    SEAL_NODE_LIMIT,
    SEAL_TEXT_BYTES_LIMIT,
)
from theater.harness.builtin.plugins.claude.observer import ClaudeCodeObserver
from theater.harness.normalization.timing import iso_epoch
from theater.models import JobState
from theater.provenance import TranscriptProvenance
from theater.transcript_identity import TRANSCRIPT_SOURCE_UNAVAILABLE_CODE

LAUNCHED = iso_epoch("2026-01-01T00:00:03Z")
SESSION = "22222222-2222-4222-8222-222222222222"


@pytest.mark.parametrize("turns", [32, 700])
@pytest.mark.parametrize("attachments", [False, True])
def test_copied_history_preserves_the_answer_and_only_projects_trajectory(turns, attachments):
    observer = ClaudeCodeObserver(launched_at=LAUNCHED)
    parsed = [
        observer.parse_record(line, i)
        for i, line in enumerate(claude_fork(turns, attachments=attachments))
    ]
    events = [event for record in parsed for event in record.events]
    assert [event.raw_text for event in events if event.turn_end] == ["NEW"]
    assert [event.raw_text for event in events if event.kind == EventKind.USER] == ["old0", "new"]
    assert (
        sum(event.kind == EventKind.USER for record in parsed for event in record.baseline_events)
        == turns + 1
    )


@pytest.mark.parametrize("conflict", ["lost_ancestor", "message_id", "uuid", "missing_uuid"])
def test_unprovable_or_conflicting_copies_never_reenter_live_events(conflict):
    lines = claude_fork(1000, attachments=True)
    observer = ClaudeCodeObserver(launched_at=LAUNCHED)
    for i, line in enumerate(lines[:-3]):
        observer.parse_record(line, i)
    copied = json.loads(
        claude_fragment(
            "branch_answer",
            "old_marker0",
            {"type": "text", "text": "COPIED"},
            "branch_msg",
        )
    )
    copied.update(timestamp="2026-01-01T00:00:00Z", sessionId="fork")
    if conflict == "message_id":
        copied.update(parentUuid="old_marker999")
        copied["message"]["id"] = "msg_new"
    elif conflict == "uuid":
        copied.update(uuid="new_answer", parentUuid="old_marker999")
    elif conflict == "missing_uuid":
        copied.pop("uuid")
    index = len(lines) - 3
    parsed = observer.parse_record(json.dumps(copied), index)
    marker = json.loads(claude_marker("branch_end", copied.get("uuid", "branch_answer")))
    marker.update(timestamp="2026-01-01T00:00:00Z", sessionId="fork")
    following = observer.parse_record(json.dumps(marker), index + 1)
    assert parsed.events == following.events == ()
    assert [event.raw_text for event in parsed.baseline_events] == ["COPIED"]


def test_later_live_turn_survives_a_dropped_marker_after_a_fork():
    lines = claude_fork(1)
    later = [
        claude_record("second_user", "new_marker", "user", message={"content": "second"}),
        claude_fragment("second_answer", "unread", {"type": "text", "text": "SECOND"}, "m2"),
        claude_marker("second_marker", "second_answer"),
        claude_record("third_user", "second_marker", "user", message={"content": "third"}),
        claude_fragment("third_answer", "third_user", {"type": "text", "text": "THIRD"}, "m3"),
        claude_marker("third_marker", "third_answer"),
    ]
    lines.extend(
        json.dumps({**json.loads(line), "timestamp": "2026-01-01T00:00:05Z", "sessionId": "fork"})
        for line in later
    )
    # The replay fence survives dropping an unrelated live marker.
    lines.extend(claude_fork(1)[-3:-1])
    observer = ClaudeCodeObserver(launched_at=LAUNCHED)
    events = [event for i, line in enumerate(lines) for event in observer.parse(line, i)]
    assert [event.raw_text for event in events if event.turn_end] == ["NEW", "THIRD"]
    assert observer.stream_error is None


def test_eviction_preserves_required_nodes_only():
    lines = [
        claude_record("user", None, "user", message={"content": "go"}),
        claude_fragment("answer", "late", {"type": "text", "text": "ANSWER"}),
        claude_marker("marker", "answer"),
    ]
    lines.extend(claude_record(f"metadata{i}", None, "attachment") for i in range(SEAL_NODE_LIMIT))
    lines.append(claude_record("late", "user", "attachment"))
    observer = ClaudeCodeObserver()
    events = [event for i, line in enumerate(lines) for event in observer.parse(line, i)]
    assert [event.raw_text for event in events if event.turn_end] == ["ANSWER"]
    assert observer.stream_error is None
    assert len(observer._seal._nodes) <= SEAL_NODE_LIMIT


def test_completed_messages_yield_the_text_budget():
    observer = ClaudeCodeObserver()
    answers = []
    for number in range(33):
        text = "x" * (SEAL_TEXT_BYTES_LIMIT // 32) if number < 32 else "OK"
        lines = [
            claude_record(f"u{number}", None, "user", message={"content": "go"}),
            claude_fragment(
                f"a{number}", f"u{number}", {"type": "text", "text": text}, f"m{number}"
            ),
            claude_marker(f"e{number}", f"a{number}"),
        ]
        answers.extend(
            event.raw_text
            for i, line in enumerate(lines, number * 3)
            for event in observer.parse(line, i)
            if event.turn_end
        )
    assert len(answers) == 33 and answers[-1] == "OK"
    assert observer.stream_error is None


def _write(path, lines, *, append=False):
    with path.open("a" if append else "w") as handle:
        for line in lines:
            record = json.loads(line)
            record["sessionId"] = SESSION
            handle.write(json.dumps(record) + "\n")


@pytest.mark.parametrize("restored", [False, True], ids=["fresh", "restart"])
async def test_a_valid_active_turn_can_attach_to_a_long_transcript(tmp_path, registry, restored):
    root = tmp_path / "projects"
    path = root / "project" / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True)
    local = [
        claude_record("user", None, "user", message={"content": "go"}),
        claude_fragment("answer", "user", {"type": "text", "text": "RIGHT"}),
    ]
    local = [
        json.dumps({**json.loads(line), "timestamp": "2026-01-01T00:00:05Z"}) for line in local
    ]
    _write(path, [*["{}"] * (SEAL_NODE_LIMIT + 1), *local])
    reader = ClaudeCodeObserver(root=root).open_source_for(
        participant_id="claude",
        cwd=str(tmp_path),
        session_id=SESSION,
        session_provenance=TranscriptProvenance.EXACT,
        after=LAUNCHED,
        known_location=str(path) if restored else None,
    )
    if not restored:
        reader.admit_exact_location(location=str(path), session_id=SESSION)
    participant = registry.create_spawned(harness="claude", cwd=str(tmp_path))
    jobs = JobManager(registry.store)
    jobs.create(
        handle="active", caller_id="caller", target_id=participant.id, kind="send", prompt="go"
    )
    now = [participant.created_at]
    daemon = Observer(registry, harnesses={}, jobs=jobs, wall_clock=lambda: now[0])
    clock, turns = QuietClock(), TurnAccumulator()
    try:
        attached = await reader.read()
        assert attached.error_code is None
        assert daemon._accept_attachment(participant.id, reader, attached)
        batch = await reader.read()
        apply_batch(daemon, participant.id, batch, clock, turns, source=reader)
        now[0] += 31
        daemon._update_source_error(participant.id, await reader.read())
        assert jobs.get("active").state == JobState.RUNNING
        marker = json.loads(claude_marker("end", "answer"))
        marker["timestamp"] = "2026-01-01T00:00:05Z"
        _write(path, [json.dumps(marker)], append=True)
        apply_batch(daemon, participant.id, await reader.read(), clock, turns, source=reader)
        assert (jobs.get("active").state, jobs.get("active").result) == (JobState.DONE, "RIGHT")
    finally:
        await reader.aclose()


@pytest.mark.parametrize("timing", ["stream", "late", "restart"])
async def test_a_copied_answer_cannot_complete_a_resumed_job(tmp_path, registry, timing):
    root = tmp_path / "projects"
    path = root / "project" / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True)
    lines = claude_fork(700, attachments=True)
    _write(path, lines[:1] if timing == "stream" else lines[:-3])
    reader = ClaudeCodeObserver(root=root).open_source_for(
        participant_id="claude",
        cwd=str(tmp_path),
        session_id=SESSION,
        session_provenance=TranscriptProvenance.EXACT,
        after=LAUNCHED,
        known_location=str(path) if timing == "restart" else None,
    )
    if timing != "restart":
        reader.admit_exact_location(location=str(path), session_id=SESSION)
    participant = registry.create_spawned(harness="claude", cwd=str(tmp_path))
    jobs = JobManager(registry.store)
    jobs.create(
        handle="resume", caller_id="caller", target_id=participant.id, kind="send", prompt="new"
    )
    clock_now = [participant.created_at]
    daemon = Observer(registry, harnesses={}, jobs=jobs, wall_clock=lambda: clock_now[0])
    try:
        attached = await reader.read()
        if timing != "stream":
            assert attached.attached is None
            assert attached.waiting and attached.error_code == TRANSCRIPT_SOURCE_UNAVAILABLE_CODE
            assert jobs.get("resume").state == JobState.RUNNING
            again = await reader.read()
            assert again.waiting and again.error_code == attached.error_code
            daemon._update_source_error(participant.id, attached)
            clock_now[0] += 31
            daemon._update_source_error(participant.id, again)
            failed = jobs.get("resume")
            assert (failed.state, failed.result, failed.error_code) == (
                JobState.CRASHED,
                "",
                TRANSCRIPT_SOURCE_UNAVAILABLE_CODE,
            )
            await _assert_recovery(reader, path, daemon, participant.id, timing)
            assert jobs.get("resume").state == JobState.CRASHED
            return
        assert daemon._accept_attachment(participant.id, reader, attached)
        _write(path, lines[1:], append=True)
        clock, turns = QuietClock(), TurnAccumulator()
        ends = []
        while reader.offset < path.stat().st_size:
            batch = await reader.read()
            ends.extend(event.raw_text for event in batch.events if event.turn_end)
            apply_batch(daemon, participant.id, batch, clock, turns, source=reader)
        assert ends == ["NEW"]
        assert (jobs.get("resume").state, jobs.get("resume").result) == (JobState.DONE, "NEW")
    finally:
        await reader.aclose()


@pytest.mark.parametrize("written", [3, 43], ids=["before-history", "mid-turn"])
async def test_history_copied_in_after_the_attach_never_answers_a_resumed_job(
    tmp_path, registry, written
):
    """Claude creates a fork's file first, then copies the history in with the first turn.

    An attach inside that gap must not let the copy count as live turns: written=3 crashes the
    job on unmatched turns, written=43 ends a half-read turn with no heard text, answering it.
    """
    headers = [
        json.dumps({"type": name}) for name in ("mode", "permission-mode", "file-history-snapshot")
    ]
    history, parent = [], None
    for number in range(33):
        user, answer, marker = f"old_user{number}", f"old_answer{number}", f"old_marker{number}"
        history += [
            claude_record(user, parent, "user", message={"content": f"old{number}"}),
            claude_fragment(answer, user, {"type": "text", "text": f"OLD{number}"}, f"m{number}"),
            claude_marker(marker, answer),
        ]
        parent = marker
    history = [
        json.dumps({**json.loads(line), "timestamp": "2026-01-01T00:00:00Z"}) for line in history
    ]
    current = [
        claude_record("new_user", parent, "user", message={"content": "new"}),
        claude_fragment("new_answer", "new_user", {"type": "text", "text": "NEW"}, "m_new"),
        claude_marker("new_marker", "new_answer"),
    ]
    lines = [
        *headers,
        *history,
        *(
            json.dumps({**json.loads(line), "timestamp": "2026-01-01T00:00:05Z"})
            for line in current
        ),
    ]
    root = tmp_path / "projects"
    path = root / "project" / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True)
    _write(path, lines[:written])
    reader = ClaudeCodeObserver(root=root).open_source_for(
        participant_id="claude",
        cwd=str(tmp_path),
        session_id=SESSION,
        session_provenance=TranscriptProvenance.EXACT,
        after=LAUNCHED,
    )
    reader.admit_exact_location(location=str(path), session_id=SESSION)
    participant = registry.create_spawned(harness="claude", cwd=str(tmp_path))
    jobs = JobManager(registry.store)
    jobs.create(
        handle="resume", caller_id="caller", target_id=participant.id, kind="send", prompt="new"
    )
    daemon = Observer(registry, harnesses={}, jobs=jobs)
    clock, turns = QuietClock(), TurnAccumulator()
    try:
        attached = await reader.read()
        assert daemon._accept_attachment(participant.id, reader, attached)
        _write(path, lines[written:], append=True)
        while reader.offset < path.stat().st_size:
            apply_batch(daemon, participant.id, await reader.read(), clock, turns, source=reader)
        assert (jobs.get("resume").state, jobs.get("resume").result) == (JobState.DONE, "NEW")
    finally:
        await reader.aclose()


async def _assert_recovery(reader, path, daemon, participant_id, timing):
    _write(path, claude_fork(700, attachments=True)[-3:], append=True)
    assert (await reader.read()).error_code == TRANSCRIPT_SOURCE_UNAVAILABLE_CODE
    later = [
        claude_record("later_user", "new_marker", "user", message={"content": "later"}),
        claude_fragment(
            "later_answer", "later_user", {"type": "text", "text": "LATER"}, "later_msg"
        ),
        claude_marker("later_marker", "later_answer"),
    ]
    later = [
        json.dumps({**json.loads(line), "timestamp": "2026-01-01T00:00:05Z"}) for line in later
    ]
    _write(path, later, append=True)
    recovered = await reader.read()
    assert daemon._accept_attachment(participant_id, reader, recovered)
    if timing == "restart":
        assert recovered.attached.last_event.raw_text == "LATER"
    else:
        live = await reader.read()
        assert [event.raw_text for event in live.events if event.turn_end] == ["LATER"]
        assert not any(event.raw_text.startswith("OLD") for event in live.events)


@pytest.mark.parametrize("pressure", ["nodes", "text"])
async def test_lost_seal_storage_is_a_persistent_source_error(tmp_path, pressure):
    root = tmp_path / "projects"
    path = root / "project" / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True)
    lines = claude_fork(1, attachments=True)
    _write(path, lines[:1])
    reader = ClaudeCodeObserver(root=root).open_source_for(
        participant_id="claude",
        cwd=str(tmp_path),
        session_id=SESSION,
        session_provenance=TranscriptProvenance.EXACT,
        after=LAUNCHED,
        known_location=str(path),
    )
    try:
        assert (await reader.read()).attached is not None
        reader.commit_attachment()
        if pressure == "nodes":
            # Every link extends the held answer's ancestry; none is expendable cache.
            extra = [
                claude_record("att2" if i == 0 else f"link{i}", f"link{i + 1}", "attachment")
                for i in range(SEAL_NODE_LIMIT)
            ]
            extra = [
                json.dumps({**json.loads(line), "timestamp": "2026-01-01T00:00:05Z"})
                for line in extra
            ]
            pending = lines[1:-3]
        else:
            extra = [
                claude_fragment("huge", None, {"type": "text", "text": "x" * (2 << 20)}, "huge_msg")
            ]
            pending = []
        _write(path, [*pending, *extra], append=True)
        while reader.offset < path.stat().st_size:
            batch = await reader.read()
        assert batch.waiting and batch.error_code == TRANSCRIPT_SOURCE_UNAVAILABLE_CODE
        assert batch.events == ()
        again = await reader.read()
        assert again.waiting and again.error_code == batch.error_code
    finally:
        await reader.aclose()


async def test_an_unread_attachment_waits_without_enabling_quiet_rescue(tmp_path):
    root = tmp_path / "projects"
    path = root / "project" / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True)
    lines = claude_fork(32, attachments=True)
    _write(path, lines[:1])
    reader = ClaudeCodeObserver(root=root).open_source_for(
        participant_id="claude",
        cwd=str(tmp_path),
        session_id=SESSION,
        session_provenance=TranscriptProvenance.EXACT,
        after=LAUNCHED,
        known_location=str(path),
    )
    try:
        await reader.read()
        reader.commit_attachment()
        _write(path, lines[1:-1], append=True)
        while reader.offset < path.stat().st_size:
            await reader.read()
        quiet = await reader.read()
        assert quiet.waiting and quiet.error_code is None and not quiet.events
        _write(path, lines[-1:], append=True)
        sealed = await reader.read()
        assert [event.raw_text for event in sealed.events if event.turn_end] == ["NEW"]
        assert not (await reader.read()).waiting
    finally:
        await reader.aclose()
