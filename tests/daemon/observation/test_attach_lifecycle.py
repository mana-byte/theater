"""Attaching to a transcript, status settling, and watch admission."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from tests.support.observation import bus_kinds
from tests.support.shipped import VibeHarness
from tests.support.vibe_transcripts import DONE, RESULT, USER, WORKING, append
from tests.support.waiting import until
from theater.daemon.observation.failures import FailureTracker
from theater.daemon.observation.service import Observer
from theater.daemon.observer import QuietClock, TurnAccumulator
from theater.daemon.schema import participants as participants_table
from theater.daemon.schema import usage as usage_table
from theater.harness.base import Event, EventKind, TokenUsage
from theater.harness.contracts.source import Batch
from theater.harness.source import Source
from theater.models import (
    Status,
)


def test_source_checkpoint_is_persisted_only_after_apply_succeeds(registry, monkeypatch):
    """A restart may skip only records whose reducer effects are durable."""

    class CheckpointSource(Source):
        checkpoint = '{"location":"/tmp/pi.jsonl","offset":42}'

        def __init__(self) -> None:
            self.acknowledged = False
            self.rolled_back = False
            self.pending = True

        async def read(self) -> Batch:
            return Batch()

        def source_checkpoint(self) -> str | None:
            return self.checkpoint if self.acknowledged else None

        def pending_source_checkpoint(self) -> str | None:
            return self.checkpoint if self.pending else None

        def acknowledge_source_checkpoint(self) -> None:
            self.acknowledged = True
            self.pending = False

        def rollback_source_checkpoint(self) -> None:
            self.rolled_back = True

    participant = registry.register(harness="vibe", pane=None, cwd="/tmp")
    observer = Observer(registry, harnesses={})
    source = CheckpointSource()
    apply = observer._reducer.apply
    persist_checkpoint = observer.store.set_source_checkpoint

    assert observer._apply_source_batch(
        participant.id, source, Batch(progressed=True), QuietClock(), TurnAccumulator()
    )
    assert source.acknowledged is True
    assert source.rolled_back is False
    assert registry.get(participant.id).source_checkpoint == source.checkpoint

    failing_source = CheckpointSource()

    def fail_apply(*_args, **_kwargs):
        raise RuntimeError("reducer failed")

    monkeypatch.setattr(observer._reducer, "apply", fail_apply)
    with pytest.raises(RuntimeError, match="reducer failed"):
        observer._apply_source_batch(
            participant.id, failing_source, Batch(progressed=True), QuietClock(), TurnAccumulator()
        )
    assert failing_source.acknowledged is False
    assert failing_source.rolled_back is True
    assert registry.get(participant.id).source_checkpoint == source.checkpoint

    persistence_failure = CheckpointSource()
    monkeypatch.setattr(observer._reducer, "apply", apply)

    def fail_persistence(*_args, **_kwargs):
        raise RuntimeError("checkpoint persistence failed")

    monkeypatch.setattr(observer.store, "set_source_checkpoint", fail_persistence)
    persistence_batch = Batch(
        events=(Event(kind=EventKind.ASSISTANT, text="apply exactly once"),), progressed=True
    )
    with pytest.raises(RuntimeError, match="checkpoint persistence failed"):
        observer._apply_source_batch(
            participant.id,
            persistence_failure,
            persistence_batch,
            QuietClock(),
            TurnAccumulator(),
        )
    assert persistence_failure.acknowledged is False
    assert persistence_failure.rolled_back is True
    assert persistence_failure.pending is True
    assert registry.get(participant.id).source_checkpoint == source.checkpoint
    assert bus_kinds(registry.store).count("agent.assistant") == 0

    monkeypatch.setattr(observer.store, "set_source_checkpoint", persist_checkpoint)
    assert observer._persist_pending_source_checkpoint(participant.id, persistence_failure)
    assert persistence_failure.acknowledged is True
    assert persistence_failure.pending is False
    assert bus_kinds(registry.store).count("agent.assistant") == 0


async def test_new_records_reach_the_bus_as_normalized_events(registry, vibe_tree, observing):
    p = registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    assert await until(lambda: "agent.transcript" in bus_kinds(registry.store))

    append(vibe_tree["transcript"], USER, WORKING, RESULT, DONE)

    assert await until(lambda: "agent.assistant" in bus_kinds(registry.store))
    assert bus_kinds(registry.store) == [
        "agent.transcript",
        "agent.user",
        "agent.assistant",
        "agent.tool_call",
        "agent.tool_result",
        "agent.assistant",
    ]
    rows = [r for r in registry.store.bus_tail(limit=500) if r["kind"] == "agent.tool_call"]
    assert rows[0]["payload"]["tool"] == "bash"
    assert rows[0]["from_id"] == p.id
    # Vibe keeps no clock of its own; the payload says so rather than lying.
    assert rows[0]["payload"]["ts"] is None
    assert rows[0]["payload"]["observed_at"] > 0
    assert rows[0]["ts"] > 0


async def test_status_follows_the_turn_boundary(registry, vibe_tree, observing):
    p = registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    assert await until(lambda: "agent.transcript" in bus_kinds(registry.store))

    append(vibe_tree["transcript"], USER, WORKING)
    assert await until(lambda: registry.get(p.id).status is Status.WORKING)

    append(vibe_tree["transcript"], RESULT, DONE)
    assert await until(lambda: registry.get(p.id).status is Status.IDLE)


async def test_attaching_skips_history_instead_of_replaying_it(registry, vibe_tree, observing):
    """An adopted agent can have megabytes behind it. None of it is news."""
    append(vibe_tree["transcript"], USER, WORKING, RESULT, DONE)
    registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))

    assert await until(lambda: "agent.transcript" in bus_kinds(registry.store))
    await asyncio.sleep(0.1)
    assert bus_kinds(registry.store) == ["agent.transcript"]

    attach = next(r for r in registry.store.bus_tail(limit=500) if r["kind"] == "agent.transcript")
    assert attach["payload"]["skipped_records"] == 4

    append(vibe_tree["transcript"], USER)
    assert await until(lambda: "agent.user" in bus_kinds(registry.store))


async def test_attaching_derives_status_from_the_last_skipped_record(
    registry, vibe_tree, observing
):
    """A spawned agent that finished before we attached must not stay idle.

    The bus gets no history replayed, but the status must reflect the last
    record seen at attach time. This is the bug that left every spawned
    participant stuck at "idle" in the real run: the agent completed its
    turn in the 2 seconds it took the observer to find the transcript, no new
    bytes arrived after attach, and _drain never fired.
    """
    append(vibe_tree["transcript"], USER, WORKING, RESULT, DONE)
    p = registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))

    assert await until(lambda: "agent.transcript" in bus_kinds(registry.store))
    assert await until(lambda: registry.get(p.id).status is Status.IDLE)
    # The bus carries only the transcript attach event — no history replay.
    assert bus_kinds(registry.store) == ["agent.transcript"]


async def test_attaching_to_a_working_agent_sets_working(registry, vibe_tree, observing):
    """If the last record is a tool call (no turn_end), status is WORKING."""
    append(vibe_tree["transcript"], USER, WORKING)
    p = registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))

    assert await until(lambda: "agent.transcript" in bus_kinds(registry.store))
    assert await until(lambda: registry.get(p.id).status is Status.WORKING)


async def test_the_harness_session_id_is_recorded_on_the_participant(
    registry, vibe_tree, observing
):
    p = registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    assert await until(lambda: registry.get(p.id).session_id == "deadbeef-1111-2222-3333")


async def test_a_half_written_record_is_not_parsed_until_it_is_complete(
    registry, vibe_tree, observing
):
    registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    assert await until(lambda: "agent.transcript" in bus_kinds(registry.store))

    with vibe_tree["transcript"].open("a") as fh:
        fh.write(json.dumps(USER)[:20])
        fh.flush()
    await asyncio.sleep(0.1)
    assert bus_kinds(registry.store) == ["agent.transcript"]

    with vibe_tree["transcript"].open("a") as fh:
        fh.write(json.dumps(USER)[20:] + "\n")
        fh.flush()
    assert await until(lambda: "agent.user" in bus_kinds(registry.store))


async def test_a_truncated_transcript_is_re_read_from_the_top(registry, vibe_tree, observing):
    registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    assert await until(lambda: "agent.transcript" in bus_kinds(registry.store))
    append(vibe_tree["transcript"], USER)
    assert await until(lambda: bus_kinds(registry.store).count("agent.user") == 1)

    vibe_tree["transcript"].write_text("")
    append(vibe_tree["transcript"], USER)
    assert await until(lambda: bus_kinds(registry.store).count("agent.user") == 2)


async def test_a_dead_participant_stops_being_watched(registry, vibe_tree, observing):
    p = registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    assert await until(lambda: p.id in observing._tasks)

    registry.mark_dead(p.id)
    assert await until(lambda: p.id not in observing._tasks)

    append(vibe_tree["transcript"], USER)
    await asyncio.sleep(0.1)
    assert "agent.user" not in bus_kinds(registry.store)


async def test_participants_of_an_unknown_harness_are_left_alone(registry, vibe_tree):
    observer = Observer(registry, {}, poll=0.01, search=0.01, sync=0.01)
    observer.start()
    try:
        registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
        await asyncio.sleep(0.1)
        assert observer._tasks == {}
        assert bus_kinds(registry.store) == []
    finally:
        await observer.aclose()


async def test_an_unobservable_participant_is_reported_once(registry, vibe_tree, observing, caplog):
    """A misreported harness name must not become a silent blind spot."""
    caplog.set_level("WARNING", logger="theater.observer")
    registry.register(harness="claude_code", pane=None, cwd=str(vibe_tree["project"]))
    assert await until(lambda: "cannot observe" in caplog.text)
    await asyncio.sleep(0.1)
    assert caplog.text.count("cannot observe") == 1


async def test_an_observer_with_no_transcript_waits_without_spinning_out(registry, tmp_path):
    """A participant we cannot find a transcript for is not an error."""
    observer = Observer(
        registry,
        {"vibe": VibeHarness(root=tmp_path / "empty")},
        poll=0.01,
        search=0.01,
        sync=0.01,
    )
    observer.start()
    try:
        p = registry.register(harness="vibe", pane=None, cwd=str(tmp_path))
        assert await until(lambda: p.id in observer._tasks)
        await asyncio.sleep(0.1)
        assert not observer._tasks[p.id].done()
        assert bus_kinds(registry.store) == []
    finally:
        await observer.aclose()


def test_usage_only_events_are_persisted_without_changing_status_or_bus(registry):
    participant = registry.register(harness="codex", pane=None, cwd="/tmp")
    participant.session_id = "session-a"
    registry.store.upsert_participant(participant)
    registry.set_status(participant.id, Status.AWAITING_INPUT)
    observer = Observer(registry, harnesses={})
    event = Event(
        kind=EventKind.ASSISTANT,
        usage=TokenUsage(input_tokens=12, cost_usd=0.29, idempotency_key="native-1"),
    )
    batch = Batch(events=[event])
    bus_before = registry.store.bus_tail()

    observer._apply(
        participant.id,
        batch,
        QuietClock(),
        TurnAccumulator(),
    )
    observer._unblock_on_semantic_progress(participant.id, batch)
    observer._apply(
        participant.id,
        batch,
        QuietClock(),
        TurnAccumulator(),
    )

    rows = registry.store.conn.execute(usage_table.select()).fetchall()
    assert len(rows) == 1
    assert rows[0].usage_key == "session-a:native-1"
    assert rows[0].harness == "codex"
    assert rows[0].cost_microcents == 29_000_000
    assert registry.store.bus_tail() == bus_before
    assert registry.get(participant.id).status is Status.AWAITING_INPUT


def test_usage_identity_is_scoped_by_participant_and_session(registry):
    observer = Observer(registry, harnesses={})
    event = Event(
        kind=EventKind.ASSISTANT,
        usage=TokenUsage(input_tokens=1, idempotency_key="native-1"),
    )
    first = registry.register(harness="codex", pane=None, cwd="/tmp")
    first.session_id = "session-a"
    registry.store.upsert_participant(first)
    observer._record_usage(first.id, event)

    first.session_id = "session-b"
    registry.store.upsert_participant(first)
    observer._record_usage(first.id, event)

    second = registry.register(harness="codex", pane=None, cwd="/tmp")
    second.session_id = "session-a"
    registry.store.upsert_participant(second)
    observer._record_usage(second.id, event)

    rows = registry.store.conn.execute(usage_table.select()).fetchall()
    assert len(rows) == 3
    assert {row.usage_key for row in rows} == {
        "session-a:native-1",
        "session-b:native-1",
    }


def test_usage_keeps_harness_attribution_after_participant_gc(registry):
    observer = Observer(registry, harnesses={})
    participant = registry.register(harness="codex", pane=None, cwd="/tmp")
    event = Event(kind=EventKind.ASSISTANT, usage=TokenUsage(input_tokens=7))
    observer._record_usage(participant.id, event)

    registry.store.conn.execute(
        delete(participants_table).where(participants_table.c.id == participant.id)
    )

    [row] = registry.store.usage_by_harness(
        day_since=0.0,
        week_since=0.0,
        month_since=0.0,
    )
    assert row["harness"] == "codex"
    assert row["today"]["input_tokens"] == 7


async def test_bus_events_carry_the_source_read_time_for_observe_to_bus(
    registry, vibe_tree, observing
):
    registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    assert await until(lambda: "agent.transcript" in bus_kinds(registry.store))

    append(vibe_tree["transcript"], USER, WORKING)

    assert await until(lambda: "agent.user" in bus_kinds(registry.store))
    row = next(r for r in registry.store.bus_tail(limit=500) if r["kind"] == "agent.user")
    assert row["payload"]["read_at"] > 0
    assert row["ts"] - row["payload"]["read_at"] >= 0


def test_batches_without_read_at_publish_bus_events_without_the_field(registry):
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    observer = Observer(registry, {"vibe": VibeHarness()})
    batch = Batch(events=(Event(kind=EventKind.ASSISTANT, text="legacy"),), progressed=True)
    assert batch.read_at is None

    observer._apply(p.id, batch, QuietClock(), TurnAccumulator())

    row = next(r for r in registry.store.bus_tail(limit=50) if r["kind"] == "agent.assistant")
    assert "read_at" not in row["payload"]


@pytest.mark.parametrize("retirement", ["dead", "shutdown"])
async def test_restart_does_not_reopen_after_retirement(registry, tmp_path, retirement):
    participant = registry.register(harness="vibe", pane=None, cwd=str(tmp_path))
    observer = Observer(registry, {"vibe": VibeHarness(root=tmp_path)})
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()
    starts = []

    async def watch(pid, _harness):
        starts.append(pid)
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cleaning.set()
            await release.wait()

    observer._watch = watch
    try:
        observer._start_watch(participant.id)
        await entered.wait()
        observer._on_live_change(participant.id)
        await cleaning.wait()
        if retirement == "dead":
            registry.mark_dead(participant.id)
        else:
            observer._stopping.set()
        release.set()
        await asyncio.gather(*tuple(observer._restarts))
        assert starts == [participant.id]
        assert participant.id not in observer._tasks
    finally:
        release.set()
        await observer.aclose()


async def test_scheduled_watch_does_not_open_a_retired_source(registry, tmp_path):
    participant = registry.register(harness="vibe", pane=None, cwd=str(tmp_path))
    opened = []

    def source_factory(*_args, **_kwargs):
        opened.append(participant.id)

    observer = Observer(
        registry, {"vibe": VibeHarness(root=tmp_path)}, source_factory=source_factory
    )
    try:
        observer._start_watch(participant.id)
        registry.mark_dead(participant.id)
        await observer._tasks[participant.id]
        assert opened == []
    finally:
        await observer.aclose()


async def test_rebuild_timing_does_not_repeat_birth_readiness(registry, tmp_path, caplog):
    observer = Observer(registry, {"vibe": VibeHarness(root=tmp_path)})
    participant = registry.register(harness="vibe", pane=None, cwd=str(tmp_path))
    entered = asyncio.Event()

    async def watch(_pid, _harness):
        entered.set()
        await asyncio.Future()

    observer._watch = watch
    caplog.set_level("DEBUG", logger="theater.timing")
    try:
        observer._start_watch(participant.id)
        await entered.wait()
        observer._on_live_change(participant.id)
        await asyncio.gather(*tuple(observer._restarts))
        operations = [getattr(record, "theater.operation", None) for record in caplog.records]
        assert operations.count("OBSERVER_WATCH") == 1
        assert operations.count("OBSERVER_RESTART") == 1
    finally:
        await observer.aclose()

    restarted = Observer(registry, {"vibe": VibeHarness(root=tmp_path)})
    restarted._watch = watch
    try:
        restarted._start_watch(participant.id)
        operations = [getattr(record, "theater.operation", None) for record in caplog.records]
        assert operations.count("OBSERVER_WATCH") == 1
    finally:
        await restarted.aclose()


async def test_finished_watch_rebuild_records_birth_readiness_once(
    registry, tmp_path, monkeypatch, caplog
):
    observer = Observer(registry, {"vibe": VibeHarness(root=tmp_path)})
    participant = registry.register(harness="vibe", pane=None, cwd=str(tmp_path))
    readiness: list[str] = []
    starts = 0

    async def watch(_pid, _harness):
        nonlocal starts
        starts += 1
        if starts > 1:
            await asyncio.Future()

    monkeypatch.setattr(
        "theater.daemon.observation.supervision.timing.ready_lag",
        lambda _operation, participant_id, *_args, **_kwargs: readiness.append(participant_id),
    )
    observer._watch = watch
    caplog.set_level("DEBUG", logger="theater.timing")
    try:
        observer._start_watch(participant.id)
        await observer._tasks[participant.id]
        observer._tasks.pop(participant.id)

        await observer._restart_watch(participant.id)

        assert readiness == [participant.id]
        operations = [getattr(record, "theater.operation", None) for record in caplog.records]
        assert operations.count("OBSERVER_RESTART") == 1
    finally:
        await observer.aclose()


async def test_first_watch_from_live_change_is_not_a_restart(registry, tmp_path, caplog):
    observer = Observer(registry, {"vibe": VibeHarness(root=tmp_path)})
    participant = registry.register(harness="vibe", pane=None, cwd=str(tmp_path))

    async def watch(_pid, _harness):
        await asyncio.Future()

    observer._watch = watch
    caplog.set_level("DEBUG", logger="theater.timing")
    try:
        observer._on_live_change(participant.id)
        await asyncio.gather(*tuple(observer._restarts))

        operations = [getattr(record, "theater.operation", None) for record in caplog.records]
        assert operations.count("OBSERVER_WATCH") == 1
        assert operations.count("OBSERVER_RESTART") == 0
    finally:
        await observer.aclose()


async def test_dead_participant_releases_readiness_bookkeeping(registry, tmp_path):
    observer = Observer(registry, {"vibe": VibeHarness(root=tmp_path)})
    participant = registry.register(harness="vibe", pane=None, cwd=str(tmp_path))

    async def watch(_pid, _harness):
        await asyncio.Future()

    observer._watch = watch
    try:
        observer._start_watch(participant.id)
        assert participant.id in observer._readiness_recorded

        registry.mark_dead(participant.id)
        observer._reconcile()
        await asyncio.gather(*tuple(observer._restarts))

        assert participant.id not in observer._readiness_recorded
    finally:
        await observer.aclose()


def test_skipped_record_is_reported_but_never_crashes_a_running_job():
    bus: list[tuple[str, dict]] = []
    job = SimpleNamespace(handle="job-a", created_at=0.0)
    store = SimpleNamespace(
        bus_append=lambda kind, *, to_id, payload: bus.append((kind, payload)),
        running_jobs_for_target=lambda _pid: [job],
    )
    tracker = FailureTracker(
        store, None, wall_now_fn=lambda: 1_000.0, grace_fn=lambda: 1.0, jobs_fn=object
    )
    finished: list[str] = []

    tracker.handle_source_error(
        "participant-a",
        Batch(error_code="pi_transcript_oversized_record", error="record too large"),
        finish_fn=lambda handle, *_args, **_kwargs: finished.append(handle),
    )

    assert finished == []
    assert not tracker.has_source_error("participant-a", "pi_transcript_oversized_record")
    assert bus == [
        (
            "agent.observation_error",
            {"code": "pi_transcript_oversized_record", "message": "record too large"},
        )
    ]
