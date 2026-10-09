"""Short transaction boundaries for cooperating Store writers."""

from __future__ import annotations

import pytest
from sqlalchemy import event as sqlalchemy_event

from theater.daemon.observation.reducer import QuietClock
from theater.daemon.observation.service import Observer
from theater.daemon.observation.turns import TurnAccumulator
from theater.daemon.persistence.store import Store
from theater.daemon.registry import Registry
from theater.harness import Event, EventKind
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
