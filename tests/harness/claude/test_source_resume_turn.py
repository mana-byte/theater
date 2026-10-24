"""Launch-local turns stay live across attachment and restored pins stay history."""

from __future__ import annotations

import json

import pytest

from tests.support.claude_seal import claude_fragment, claude_marker, claude_record
from tests.support.observation import apply_batch
from theater.daemon.jobs import JobManager
from theater.daemon.observer import Observer, QuietClock, TurnAccumulator
from theater.harness import EventKind
from theater.harness.builtin.plugins.claude import source as claude_source
from theater.harness.builtin.plugins.claude.observer import ClaudeCodeObserver
from theater.harness.normalization.timing import iso_epoch
from theater.models import JobState
from theater.provenance import TranscriptProvenance
from theater.resume_floor import encode_floor

SESSION = "22222222-2222-4222-8222-222222222222"
BEFORE = "2026-01-01T00:00:00Z"
LAUNCH = "2026-01-01T00:00:01Z"
AFTER = "2026-01-01T00:00:02Z"


def _write(path, lines, *, append=False):
    with path.open("a" if append else "w") as handle:
        for line, timestamp in lines:
            record = json.loads(line)
            record.update(timestamp=timestamp, sessionId=SESSION)
            handle.write(json.dumps(record) + "\n")


@pytest.mark.parametrize("timing", ["fast", "held", "restored_pin"])
async def test_fork_turn_is_live_but_restored_history_stays_suppressed(tmp_path, registry, timing):
    root = tmp_path / "projects"
    path = root / "project" / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True)
    observer = ClaudeCodeObserver(root=root)
    prior = [
        (claude_record("old_user", None, "user", message={"content": "old"}), BEFORE),
        (claude_fragment("old_answer", "old_user", {"type": "text", "text": "OLD"}), BEFORE),
        (claude_marker("old_marker", "old_answer"), BEFORE),
    ]
    predecessor = path.with_name("predecessor.jsonl")
    _write(predecessor, prior)
    floor = observer.stream_floor(str(predecessor))
    current = [(claude_record("new_user", "old_marker", "user", message={"content": "new"}), AFTER)]
    answer = [
        (
            claude_fragment("new_answer", "new_user", {"type": "text", "text": "NEW"}, "msg_new"),
            AFTER,
        ),
        (claude_marker("new_marker", "new_answer"), AFTER),
    ]
    _write(path, prior + current + ([] if timing == "held" else answer))
    reader = observer.open_source_for(
        participant_id="claude",
        cwd=str(tmp_path),
        session_id=SESSION,
        session_provenance=TranscriptProvenance.EXACT,
        after=iso_epoch(LAUNCH),
        known_location=str(path) if timing == "restored_pin" else None,
    )
    if timing != "restored_pin":
        reader.admit_exact_location(location=str(path), session_id=SESSION)
    participant = registry.create_spawned(harness="claude", cwd=str(tmp_path))
    participant.resume_floor = encode_floor(floor)
    registry.store.upsert_participant(participant)
    jobs = JobManager(registry.store)
    jobs.create(
        handle="resume", caller_id="caller", target_id=participant.id, kind="send", prompt="new"
    )
    daemon = Observer(registry, harnesses={}, jobs=jobs)
    try:
        attached = await reader.read()
        assert attached.attached.point.ino != floor.ino
        assert daemon._accept_attachment(participant.id, reader, attached)
        assert jobs.get("resume").state == JobState.RUNNING
        if timing == "restored_pin":
            assert (await reader.read()).events == ()
            assert registry.store.get_participant(participant.id).resume_floor == encode_floor(
                floor
            )
            return
        batch = await reader.read()
        heard = [event.raw_text for event in batch.events if event.kind == EventKind.USER]
        clock, turns = QuietClock(), TurnAccumulator()
        apply_batch(daemon, participant.id, batch, clock, turns, source=reader)
        if timing == "held":
            assert jobs.get("resume").state == JobState.RUNNING
            _write(path, answer, append=True)
            batch = await reader.read()
            apply_batch(daemon, participant.id, batch, clock, turns, source=reader)
        assert (jobs.get("resume").state, jobs.get("resume").result) == (JobState.DONE, "NEW")
        assert [event.raw_text for event in batch.events if event.turn_end] == ["NEW"]
        assert heard == ["new"]
        assert reader.offset == path.stat().st_size
        assert (await reader.read()).events == ()
        await _assert_offline_turn_after_restart(
            reader, observer, registry, jobs, participant, path, tmp_path
        )
    finally:
        await reader.aclose()


async def _assert_offline_turn_after_restart(
    reader, observer, registry, jobs, participant, path, cwd
):
    assert registry.store.get_participant(participant.id).resume_floor is None
    await reader.aclose()
    offline = [
        (
            claude_record("offline_user", "new_marker", "user", message={"content": "offline"}),
            AFTER,
        ),
        (
            claude_fragment(
                "offline_answer",
                "offline_user",
                {"type": "text", "text": "OFFLINE"},
                "msg_offline",
            ),
            AFTER,
        ),
        (claude_marker("offline_marker", "offline_answer"), AFTER),
    ]
    jobs.create(
        handle="offline",
        caller_id="caller",
        target_id=participant.id,
        kind="send",
        prompt="offline",
    )
    _write(path, offline, append=True)
    reader = observer.open_source_for(
        participant_id=participant.id,
        cwd=str(cwd),
        session_id=SESSION,
        session_provenance=TranscriptProvenance.EXACT,
        after=iso_epoch(LAUNCH),
        known_location=registry.store.get_participant(participant.id).transcript_location,
    )
    daemon = Observer(registry, harnesses={}, jobs=jobs)
    try:
        attached = await reader.read()
        assert attached.attached.last_event.raw_text == "OFFLINE"
        assert daemon._accept_attachment(participant.id, reader, attached)
        assert (jobs.get("offline").state, jobs.get("offline").result) == (JobState.DONE, "OFFLINE")
        assert (await reader.read()).events == ()
    finally:
        await reader.aclose()


@pytest.mark.parametrize(
    "extra",
    [
        {"timestamp": None},
        {"timestamp": BEFORE},
        {"sessionId": "another-session"},
        {"isSidechain": True},
    ],
)
def test_live_turn_requires_launch_local_main_thread_evidence(extra):
    record = {"type": "user", "timestamp": AFTER, "sessionId": SESSION, **extra}
    records = [(0, 10, "{}"), (1, 20, json.dumps(record))]
    assert claude_source._live_turn_start(records, iso_epoch(LAUNCH), SESSION) is None


def test_copied_future_timestamp_exposes_wall_clock_assumption():
    predecessor = {
        "type": "user",
        "uuid": "old_user",
        "timestamp": AFTER,
        "sessionId": SESSION,
    }
    records = [(0, 10, "{}"), (1, 20, json.dumps(predecessor))]
    # Without a predecessor floor, a clock step makes copied history look launch-local.
    assert claude_source._live_turn_start(records, iso_epoch(LAUNCH), SESSION) == (1, 10)


async def test_fresh_launch_drains_two_queued_turns_in_order(tmp_path, registry):
    root = tmp_path / "projects"
    path = root / "project" / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True)
    lines = []
    parent = None
    for number in (1, 2):
        user, answer, marker = f"user_{number}", f"answer_{number}", f"marker_{number}"
        lines.extend(
            [
                (
                    claude_record(user, parent, "user", message={"content": f"prompt {number}"}),
                    AFTER,
                ),
                (
                    claude_fragment(
                        answer, user, {"type": "text", "text": f"ANSWER {number}"}, f"msg_{number}"
                    ),
                    AFTER,
                ),
                (claude_marker(marker, answer), AFTER),
            ]
        )
        parent = marker
    _write(path, lines)
    reader = ClaudeCodeObserver(root=root).open_source_for(
        participant_id="claude",
        cwd=str(tmp_path),
        session_id=SESSION,
        session_provenance=TranscriptProvenance.EXACT,
        after=iso_epoch(LAUNCH),
    )
    reader.admit_exact_location(location=str(path), session_id=SESSION)
    participant = registry.create_spawned(harness="claude", cwd=str(tmp_path))
    jobs = JobManager(registry.store)
    for number in (1, 2):
        jobs.create(
            handle=f"queued_{number}",
            caller_id="caller",
            target_id=participant.id,
            kind="send",
            prompt=f"prompt {number}",
        )
    daemon = Observer(registry, harnesses={}, jobs=jobs)
    try:
        attached = await reader.read()
        assert attached.attached.last_event is None
        assert daemon._accept_attachment(participant.id, reader, attached)
        batch = await reader.read()
        apply_batch(daemon, participant.id, batch, QuietClock(), TurnAccumulator(), source=reader)
        assert [event.raw_text for event in batch.events if event.kind == EventKind.USER] == [
            "prompt 1",
            "prompt 2",
        ]
        assert [event.raw_text for event in batch.events if event.turn_end] == [
            "ANSWER 1",
            "ANSWER 2",
        ]
        assert [
            (jobs.get(f"queued_{number}").state, jobs.get(f"queued_{number}").result)
            for number in (1, 2)
        ] == [
            (JobState.DONE, "ANSWER 1"),
            (JobState.DONE, "ANSWER 2"),
        ]
        assert reader.offset == path.stat().st_size
        assert (await reader.read()).events == ()
    finally:
        await reader.aclose()
