"""Provably write-free batches skip the write unit; everything else keeps it."""

from __future__ import annotations

import pytest

from theater.daemon.observer import Observer, QuietClock, TurnAccumulator
from theater.harness.base import Event, EventKind
from theater.harness.contracts.runtime import NativeTurnOutcome, NativeTurnTerminal
from theater.harness.source import Batch, Source


class Pending(Source):
    def __init__(self, checkpoint: str | None = None) -> None:
        self.checkpoint = checkpoint

    async def read(self) -> Batch:
        return Batch()

    def pending_source_checkpoint(self) -> str | None:
        return self.checkpoint


class Telemetry:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def record_batch(self, pid, batch, new_usage_events) -> None:
        self.calls.append((pid, batch, new_usage_events))

    def discard(self, pid) -> None:
        return None


@pytest.fixture
def unit_count(registry, monkeypatch):
    opened = []
    real = registry.store.write_unit

    def counting():
        opened.append(1)
        return real()

    monkeypatch.setattr(registry.store, "write_unit", counting)
    return opened


def apply(observer, source, batch, pid, opened):
    opened.clear()  # drop units opened by fixture setup (register)
    return observer._apply_source_batch(pid, source, batch, QuietClock(), TurnAccumulator())


def test_empty_batch_opens_no_write_unit(registry, unit_count):
    pid = registry.register(harness="vibe", pane=None, cwd="/tmp").id
    observer = Observer(registry, harnesses={})
    assert not apply(observer, Pending(), Batch(), pid, unit_count)
    assert unit_count == []


def test_batch_with_events_opens_exactly_one_unit(registry, unit_count):
    pid = registry.register(harness="vibe", pane=None, cwd="/tmp").id
    observer = Observer(registry, harnesses={})
    batch = Batch(events=(Event(kind=EventKind.ASSISTANT, text="hi"),))
    assert apply(observer, Pending(), batch, pid, unit_count)
    assert unit_count == [1]


def test_empty_batch_with_pending_checkpoint_takes_unit_path(registry, unit_count):
    pid = registry.register(harness="vibe", pane=None, cwd="/tmp").id
    observer = Observer(registry, harnesses={})
    assert not apply(observer, Pending('{"offset":1}'), Batch(), pid, unit_count)
    assert unit_count == [1]


def test_empty_batch_with_terminal_evidence_takes_unit_path(registry, unit_count):
    pid = registry.register(harness="vibe", pane=None, cwd="/tmp").id
    observer = Observer(registry, harnesses={})
    outcome = NativeTurnOutcome(
        native_session_id="s", native_turn_id="t", terminal=NativeTurnTerminal.COMPLETED
    )
    apply(observer, Pending(), Batch(terminal_evidence=(outcome,)), pid, unit_count)
    assert unit_count == [1]


def test_fast_path_still_fires_telemetry(registry, unit_count):
    telemetry = Telemetry()
    observer = Observer(registry, harnesses={}, agent_telemetry=telemetry)
    batch = Batch()
    assert not apply(observer, Pending(), batch, "missing", unit_count)
    assert unit_count == []
    assert telemetry.calls == [("missing", batch, ())]
