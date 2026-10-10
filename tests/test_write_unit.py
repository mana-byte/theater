"""Short transaction boundaries for cooperating Store writers."""

from __future__ import annotations

import pytest
from sqlalchemy import event as sqlalchemy_event

from theater.daemon.jobs import JobManager
from theater.daemon.observation.reducer import QuietClock
from theater.daemon.observation.rollback import rollback_observation_state
from theater.daemon.observation.service import Observer
from theater.daemon.observation.turns import TurnAccumulator
from theater.daemon.persistence.store import Store
from theater.daemon.registry import Registry
from theater.harness import Event, EventKind
from theater.harness.contracts.runtime import ControlDeliveryPhase, ControlKind, DeliveryResult
from theater.harness.source import Batch
from theater.models import Status


def _usage_values() -> dict:
    return {
        "participant_id": "participant-a",
        "tree_root_id": None,
        "usage_key": "usage-a",
        "ts": 1.0,
        "model": "model-a",
        "harness": "codex",
        "input_tokens": 1,
        "output_tokens": 2,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "reasoning_output_tokens": 0,
        "cost_microcents": 3,
    }


def test_supplied_autocommit_connection_batches_store_writers(tmp_path) -> None:
    store = Store(tmp_path / "write-unit.db")
    trace: list[str] = []
    notifications: list[str] = []
    store.register_bus_listener(lambda _row: notifications.append("bus"))
    store.conn.connection.driver_connection.set_trace_callback(trace.append)
    try:
        with store.write_unit(connection=store.conn) as unit:
            store.bus_append("agent.assistant", from_id="participant-a", connection=unit.connection)
            assert store.record_usage(**_usage_values(), connection=unit.connection)
            unit.after_commit(lambda: notifications.append("unit"))

        assert trace.count("BEGIN") == 1
        assert trace.count("COMMIT") == 1
        assert notifications == ["bus", "unit"]
        assert len(store.bus_tail()) == 1
        assert store.usage_totals()["output_tokens"] == 2
    finally:
        store.close()


def test_write_unit_rolls_back_and_rejects_nesting(tmp_path) -> None:
    store = Store(tmp_path / "rollback.db")
    notifications: list[str] = []
    try:
        with pytest.raises(RuntimeError, match="nested write units"):  # noqa: SIM117
            with store.write_unit(connection=store.conn) as unit:
                store.bus_append(
                    "agent.assistant", from_id="participant-a", connection=unit.connection
                )
                assert store.record_usage(**_usage_values(), connection=unit.connection)
                unit.after_commit(lambda: notifications.append("committed"))
                with store.write_unit(connection=store.conn):
                    pass

        assert store.bus_tail() == []
        assert store.usage_totals()["output_tokens"] == 0
        assert notifications == []
    finally:
        store.close()


def test_explicit_participant_connection_does_not_publish_a_journal_event(tmp_path) -> None:
    store = Store(tmp_path / "participant-raw.db")
    registry = Registry(store)
    participant = registry.register(harness="codex", pane=None, cwd="/tmp")
    participant = store.get_participant(participant.id)
    assert participant is not None
    before = store.journal.current_sequence()
    participant.description = "stored by an external transaction"
    try:
        with store.write_unit() as unit:
            store.upsert_participant(participant, connection=unit.connection)

        assert store.journal.current_sequence() == before
    finally:
        store.close()


def test_commit_failure_rolls_back_a_supplied_connection(tmp_path, monkeypatch) -> None:
    store = Store(tmp_path / "commit-failure.db")
    notifications: list[str] = []
    commit = store.conn.commit

    def fail_commit() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(store.conn, "commit", fail_commit)
    try:
        with pytest.raises(RuntimeError, match="commit failed"):  # noqa: SIM117
            with store.write_unit(connection=store.conn) as unit:
                store.bus_append(
                    "agent.assistant", from_id="participant-a", connection=unit.connection
                )
                unit.after_commit(lambda: notifications.append("committed"))

        assert not store.conn.in_transaction()
        assert store.bus_tail() == []
        assert notifications == []
    finally:
        monkeypatch.setattr(store.conn, "commit", commit)
        store.close()


class _CheckpointSource:
    def __init__(self, checkpoint: str) -> None:
        self.checkpoint = checkpoint
        self.acknowledged = False
        self.rolled_back = False

    def pending_source_checkpoint(self) -> str:
        return self.checkpoint

    def acknowledge_source_checkpoint(self) -> None:
        self.acknowledged = True

    def rollback_source_checkpoint(self) -> None:
        self.rolled_back = True


def test_nonterminal_observation_batch_commits_once_with_checkpoint(tmp_path) -> None:
    store = Store(tmp_path / "observer-unit.db")
    registry = Registry(store)
    participant = registry.register(harness="codex", pane=None, cwd="/tmp")
    observer = Observer(registry, harnesses={})
    source = _CheckpointSource("cursor-a")
    commits: list[None] = []
    sqlalchemy_event.listen(store.engine, "commit", lambda _connection: commits.append(None))
    try:
        assert observer._apply_source_batch(
            participant.id,
            source,
            Batch(events=[Event(kind=EventKind.ASSISTANT, text="working")]),
            QuietClock(),
            TurnAccumulator(),
        )

        assert commits == [None]
        assert source.acknowledged
        assert store.get_participant(participant.id).status is Status.WORKING
        assert store.get_participant(participant.id).source_checkpoint == "cursor-a"
    finally:
        store.close()


def test_direct_reducer_apply_restores_watcher_state_after_rollback(tmp_path, monkeypatch) -> None:
    store = Store(tmp_path / "observer-rollback.db")
    registry = Registry(store)
    participant = registry.register(harness="codex", pane=None, cwd="/tmp")
    observer = Observer(registry, harnesses={})
    clock = QuietClock(last_text="before")
    turns = TurnAccumulator()
    bus_before = store.bus_tail()

    def fail(*_args, **_kwargs) -> None:
        raise RuntimeError("cannot persist status")

    monkeypatch.setattr(store, "set_status", fail)
    with pytest.raises(RuntimeError, match="cannot persist status"):
        observer._apply(
            participant.id,
            Batch(events=[Event(kind=EventKind.ASSISTANT, text="after")]),
            clock,
            turns,
        )

    assert clock.last_text == "before"
    assert turns.take().said == ""
    assert store.bus_tail() == bus_before


def test_checkpoint_failure_rolls_back_the_observation_batch(tmp_path, monkeypatch) -> None:
    store = Store(tmp_path / "checkpoint-rollback.db")
    registry = Registry(store)
    participant = registry.register(harness="codex", pane=None, cwd="/tmp")
    observer = Observer(registry, harnesses={})
    source = _CheckpointSource("cursor-a")
    bus_before = store.bus_tail()

    def fail(*_args, **_kwargs) -> None:
        raise RuntimeError("cannot persist checkpoint")

    monkeypatch.setattr(store, "set_source_checkpoint", fail)
    with pytest.raises(RuntimeError, match="cannot persist checkpoint"):
        observer._apply_source_batch(
            participant.id,
            source,
            Batch(events=[Event(kind=EventKind.ASSISTANT, text="working")]),
            QuietClock(),
            TurnAccumulator(),
        )

    assert source.rolled_back
    assert not source.acknowledged
    assert store.bus_tail() == bus_before


def test_terminal_completion_failure_rolls_back_the_source_checkpoint(tmp_path) -> None:
    store = Store(tmp_path / "terminal-failure.db")
    registry = Registry(store)
    participant = registry.register(harness="codex", pane=None, cwd="/tmp")
    jobs = JobManager(store)
    job = jobs.create(
        handle="job-a",
        caller_id="cli",
        target_id=participant.id,
        kind="send",
    )
    observer = Observer(registry, harnesses={}, jobs=jobs)
    source = _CheckpointSource("cursor-a")
    store.conn.exec_driver_sql(
        "CREATE TRIGGER fail_job_finish BEFORE UPDATE OF state ON jobs "
        "WHEN NEW.state != OLD.state BEGIN SELECT RAISE(ABORT, 'finish blocked'); END"
    )
    try:
        with pytest.raises(Exception, match="finish blocked"):
            observer._apply_source_batch(
                participant.id,
                source,
                Batch(events=[Event(kind=EventKind.ASSISTANT, text="done", turn_end=True)]),
                QuietClock(),
                TurnAccumulator(),
            )

        assert source.rolled_back
        assert not source.acknowledged
        assert store.get_job(job.handle).state == "running"
    finally:
        store.close()


def test_write_unit_accepts_autocommit_connection_after_reads(tmp_path) -> None:
    store = Store(tmp_path / "autobegin.db")
    try:
        store.bus_tail()
        assert store.conn.in_transaction()
        with store.write_unit(connection=store.conn) as unit:
            store.bus_append("agent.assistant", from_id="participant-a", connection=unit.connection)

        assert len(store.bus_tail()) == 1
    finally:
        store.close()


def test_rollback_journal_undoes_only_the_failed_units_mutations() -> None:
    clock, turns = QuietClock(last_text="before"), TurnAccumulator()
    turns.say("kept")
    turns.mark_handled("t0")
    with pytest.raises(RuntimeError, match="boom"), rollback_observation_state(clock, turns):
        turns.say("lost")
        turns.hear("lost")
        turns.mark_handled("t1")
        clock.last_text = "after"
        assert turns.take().said == "kept\n\nlost"
        raise RuntimeError("boom")

    assert clock.last_text == "before"
    assert not turns.already_handled("t1") and turns.already_handled("t0")
    assert turns.take().said == "kept"
    assert "take" not in turns.__dict__


class _NoCopyTurns(TurnAccumulator):
    def __deepcopy__(self, memo):
        raise AssertionError("turn history was copied")


def test_empty_observation_unit_never_copies_turn_history() -> None:
    turns = _NoCopyTurns()
    for index in range(1000):
        turns.say(f"block-{index}")
    with rollback_observation_state(QuietClock(), turns):
        pass

    assert len(turns._blocks) == 1000


def _two_delivered_jobs(registry, jobs, *, cwd=None):
    from tests.test_completion_queued_followups import _op

    participant = registry.register(harness="codex", pane=None, cwd="/tmp")
    for handle in ("job-a", "job-b"):
        jobs.create(
            handle=handle,
            caller_id="cli",
            target_id=participant.id,
            kind="send",
            prompt=f"{handle} prompt",
            cwd=cwd,
        )
    with registry.store.write_unit() as unit:
        for handle in ("job-a", "job-b"):
            registry.store.reserve_control_operation(
                _op(
                    f"op-{handle}",
                    participant.id,
                    kind=ControlKind.SEND,
                    phase=ControlDeliveryPhase.SETTLED,
                    job_handle=handle,
                    result=DeliveryResult.ACCEPTED,
                ),
                connection=unit.connection,
            )
    return participant


def _terminal_batch(prompt: str, answer: str, turn_id: str) -> Batch:
    return Batch(
        events=[
            Event(kind=EventKind.USER, text=prompt),
            Event(kind=EventKind.ASSISTANT, text=answer, turn_end=True, turn_id=turn_id),
        ]
    )


def test_terminal_batch_commits_once_and_replay_never_completes_the_next_job(
    tmp_path, monkeypatch
) -> None:
    store = Store(tmp_path / "replay.db")
    registry = Registry(store)
    jobs = JobManager(store)
    participant = _two_delivered_jobs(registry, jobs)
    store.set_status(participant.id, Status.WORKING)
    observer = Observer(registry, harnesses={}, jobs=jobs)
    clock, turns = QuietClock(), TurnAccumulator()
    batch = _terminal_batch("job-a prompt", "answer a", "turn-a")
    commits: list[None] = []
    sqlalchemy_event.listen(store.engine, "commit", lambda _connection: commits.append(None))
    set_status = store.set_status

    def fail(*_args, **_kwargs) -> None:
        raise RuntimeError("cannot persist status")

    try:
        monkeypatch.setattr(store, "set_status", fail)
        with pytest.raises(RuntimeError, match="cannot persist status"):
            observer._apply_source_batch(
                participant.id, _CheckpointSource("c"), batch, clock, turns
            )
        a_after_failure = store.get_job("job-a").state
        marker_after_failure = turns.already_handled("turn-a")

        monkeypatch.setattr(store, "set_status", set_status)
        commits.clear()
        observer._apply_source_batch(participant.id, _CheckpointSource("c"), batch, clock, turns)

        assert store.get_job("job-b").state == "running"
        assert store.get_job("job-b").result is None
        assert a_after_failure == "running"
        assert not marker_after_failure
        assert (store.get_job("job-a").state, store.get_job("job-a").result) == ("done", "answer a")
        assert len(commits) == 1
    finally:
        store.close()


@pytest.mark.parametrize("entry", ["source_batch", "direct_apply"])
def test_second_turn_touches_go_to_the_next_job_without_file_io_in_the_unit(
    tmp_path, monkeypatch, entry
) -> None:
    from sqlalchemy import select

    from theater.daemon import jobs as jobs_module
    from theater.daemon.schema import touch
    from theater.harness import EventPath

    store = Store(tmp_path / "touches.db")
    registry = Registry(store)
    jobs = JobManager(store)
    participant = _two_delivered_jobs(registry, jobs, cwd=str(tmp_path))
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    observer = Observer(registry, harnesses={}, jobs=jobs)

    def guarded(real):
        def call(*args, **kwargs):
            assert not store._db._write_unit_active, "file I/O inside the write unit"
            return real(*args, **kwargs)

        return call

    monkeypatch.setattr(jobs_module, "blob_hash", guarded(jobs_module.blob_hash))
    monkeypatch.setattr(
        jobs_module, "normalize_touch_path", guarded(jobs_module.normalize_touch_path)
    )
    batch = Batch(
        events=[
            Event(kind=EventKind.USER, text="job-a prompt"),
            Event(
                kind=EventKind.ASSISTANT,
                text="answer a",
                turn_end=True,
                turn_id="turn-a",
                paths=(EventPath("a.txt", "write"),),
            ),
            Event(kind=EventKind.USER, text="job-b prompt"),
            Event(
                kind=EventKind.ASSISTANT,
                text="answer b",
                turn_end=True,
                turn_id="turn-b",
                paths=(EventPath("b.txt", "write"),),
            ),
        ]
    )
    try:
        if entry == "source_batch":
            observer._apply_source_batch(
                participant.id, _CheckpointSource("c"), batch, QuietClock(), TurnAccumulator()
            )
        else:
            observer._apply(participant.id, batch, QuietClock(), TurnAccumulator())

        rows = store.conn.execute(select(touch.c.job_handle, touch.c.path)).fetchall()
        assert sorted(tuple(r) for r in rows) == [("job-a", "a.txt"), ("job-b", "b.txt")]
    finally:
        store.close()


def test_each_job_spends_only_its_own_touch_hash_budget(tmp_path, monkeypatch) -> None:
    from sqlalchemy import select

    from theater.daemon import jobs as jobs_module
    from theater.daemon.schema import touch
    from theater.harness import EventPath

    monkeypatch.setattr(jobs_module, "TOUCH_HASH_MAX_JOB_BYTES", 3)
    store = Store(tmp_path / "budget.db")
    registry = Registry(store)
    jobs = JobManager(store)
    participant = _two_delivered_jobs(registry, jobs, cwd=str(tmp_path))
    (tmp_path / "a.txt").write_text("aa")
    (tmp_path / "b.txt").write_text("bb")
    observer = Observer(registry, harnesses={}, jobs=jobs)
    batch = Batch(
        events=[
            Event(kind=EventKind.USER, text="job-a prompt"),
            Event(
                kind=EventKind.ASSISTANT,
                text="answer a",
                turn_end=True,
                turn_id="turn-a",
                paths=(EventPath("a.txt", "write"),),
            ),
            Event(kind=EventKind.USER, text="job-b prompt"),
            Event(
                kind=EventKind.ASSISTANT,
                text="answer b",
                turn_end=True,
                turn_id="turn-b",
                paths=(EventPath("b.txt", "write"),),
            ),
        ]
    )
    try:
        observer._apply(participant.id, batch, QuietClock(), TurnAccumulator())

        rows = store.conn.execute(
            select(
                touch.c.job_handle,
                touch.c.sha_before,
                touch.c.sha_after,
                touch.c.sha_before_error,
                touch.c.sha_after_error,
            )
        ).fetchall()
        assert len(rows) == 2
        for row in rows:
            assert row.sha_before and row.sha_after
            assert row.sha_before_error is None
            assert row.sha_after_error is None
    finally:
        store.close()


def test_dot_slash_touch_path_completes_the_job(tmp_path) -> None:
    from sqlalchemy import select

    from theater.daemon.schema import touch
    from theater.harness import EventPath

    store = Store(tmp_path / "dotslash.db")
    registry = Registry(store)
    jobs = JobManager(store)
    participant = _two_delivered_jobs(registry, jobs, cwd=str(tmp_path))
    (tmp_path / "x.txt").write_text("x")
    observer = Observer(registry, harnesses={}, jobs=jobs)
    batch = Batch(
        events=[
            Event(kind=EventKind.USER, text="job-a prompt"),
            Event(
                kind=EventKind.ASSISTANT,
                text="answer a",
                turn_end=True,
                turn_id="turn-a",
                paths=(EventPath("./x.txt", "write"),),
            ),
        ]
    )
    try:
        observer._apply(participant.id, batch, QuietClock(), TurnAccumulator())

        assert store.get_job("job-a").state == "done"
        rows = store.conn.execute(select(touch.c.job_handle, touch.c.path)).fetchall()
        assert [tuple(r) for r in rows] == [("job-a", "x.txt")]
    finally:
        store.close()
