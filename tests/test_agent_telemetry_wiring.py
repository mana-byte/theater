"""Generic observer telemetry wiring stays independent from its implementation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from theater.daemon.observer import Observer, QuietClock, TurnAccumulator
from theater.harness.base import Event, EventKind, TokenUsage
from theater.harness.source import Batch, Source


class Telemetry:
    def __init__(self, *, fail_record: bool = False, fail_discard: bool = False) -> None:
        self.calls: list[tuple[str, Batch, tuple[Event, ...]]] = []
        self.discarded: list[str] = []
        self.fail_record = fail_record
        self.fail_discard = fail_discard

    def record_batch(self, pid: str, batch: Batch, new_usage_events: tuple[Event, ...]) -> None:
        self.calls.append((pid, batch, new_usage_events))
        if self.fail_record:
            raise RuntimeError("record failed")

    def discard(self, pid: str) -> None:
        self.discarded.append(pid)
        if self.fail_discard:
            raise RuntimeError("discard failed")


def usage_event(key: str) -> Event:
    return Event(
        kind=EventKind.ASSISTANT,
        usage=TokenUsage(input_tokens=1, idempotency_key=key),
    )


def test_observer_without_telemetry_keeps_existing_apply_result(registry):
    observer = Observer(registry, harnesses={})
    assert observer._apply("missing", Batch(progressed=True), QuietClock(), TurnAccumulator())
    assert not observer._apply("missing", Batch(), QuietClock(), TurnAccumulator())


def test_telemetry_receives_only_new_usage_events(registry):
    telemetry = Telemetry()
    observer = Observer(registry, harnesses={}, agent_telemetry=telemetry)
    participant = registry.register(harness="codex", pane="%1", cwd="/tmp")
    first = usage_event("same")
    second = usage_event("same")
    batch = Batch(events=[first, second])

    assert observer._apply(participant.id, batch, QuietClock(), TurnAccumulator())

    assert telemetry.calls == [(participant.id, batch, (first,))]


def test_telemetry_receives_accepted_usage_free_batch_once(registry):
    telemetry = Telemetry()
    observer = Observer(registry, harnesses={}, agent_telemetry=telemetry)
    batch = Batch(progressed=True)

    assert observer._apply("missing", batch, QuietClock(), TurnAccumulator())

    assert telemetry.calls == [("missing", batch, ())]


def test_telemetry_failure_does_not_change_apply_result(registry, caplog):
    observer = Observer(registry, harnesses={}, agent_telemetry=Telemetry(fail_record=True))

    assert observer._apply("missing", Batch(progressed=True), QuietClock(), TurnAccumulator())
    assert "agent telemetry failed for missing" in caplog.text


class OneBatchSource(Source):
    def __init__(self, observer: Observer, batch: Batch) -> None:
        self.observer = observer
        self.batch = batch
        self.closed = False

    async def read(self) -> Batch:
        self.observer._stopping.set()
        return self.batch

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_unaccepted_batch_is_not_recorded_and_teardown_discards(registry):
    telemetry = Telemetry()
    observer = Observer(
        registry,
        {"fake": SimpleNamespace(observer=SimpleNamespace(has_transcript=True))},
        agent_telemetry=telemetry,
    )
    participant = registry.register(harness="fake", pane=None, cwd="/tmp")
    source = OneBatchSource(observer, Batch(events=[usage_event("unaccepted")], progressed=True))
    observer._open_source = lambda *_: source
    observer._accept_attachment = lambda *_: False

    await observer._watch(participant.id, "fake")

    assert telemetry.calls == []
    assert telemetry.discarded == [participant.id]
    assert source.closed


@pytest.mark.asyncio
async def test_telemetry_discard_failure_is_swallowed(registry, caplog):
    telemetry = Telemetry(fail_discard=True)
    observer = Observer(
        registry,
        {"fake": SimpleNamespace(observer=SimpleNamespace(has_transcript=True))},
        agent_telemetry=telemetry,
    )
    participant = registry.register(harness="fake", pane=None, cwd="/tmp")
    source = OneBatchSource(observer, Batch())
    observer._open_source = lambda *_: source

    await observer._watch(participant.id, "fake")

    assert telemetry.discarded == [participant.id]
    assert "discarding agent telemetry failed" in caplog.text
