"""Copied history never evicts a fork's answer or becomes a live turn."""

from __future__ import annotations

import json

import pytest

from tests.support.claude_seal import claude_fork, claude_fragment, claude_marker, claude_record
from tests.support.observation import apply_batch
from theater.daemon.jobs import JobManager
from theater.daemon.observer import Observer, QuietClock, TurnAccumulator
from theater.harness import EventKind
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


def _write(path, lines, *, append=False):
    with path.open("a" if append else "w") as handle:
        for line in lines:
            record = json.loads(line)
            record["sessionId"] = SESSION
            handle.write(json.dumps(record) + "\n")


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
            extra = [claude_record(f"stray{i}", None, "attachment") for i in range(2048)]
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
