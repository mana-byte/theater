"""Focused Wave 5 observability regressions: inspected fields, not behavior.

Every test here spies the process's one metric bridge and asserts the exact
metric names and bounded attribute sets the Wave 5 instrumentation emits —
control latency across every public kind and accepted/rejected/unknown/queued
outcome, the unknown-delivery counter and its fixed reason vocabulary, the
runtime-reconnect span, the aggregate queue-depth gauge source, and the
live observation gap with its per-watch reset. Nothing here depends on
timing beyond ordering, and every failure-isolation test proves control
behavior is untouched when the bridge itself is broken.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tests.rig.fake_runtime import FakeRuntime
from tests.test_control_service import (
    Harness,
    _make_operation,
    disconnected_native_harness,
    make_runtime,
    open_harness,
    state_of,
    wrap_runtime,
)
from tests.test_live_observation_integration import Rig, bus_kinds, until
from theater.constants.observability import CONTROL_QUEUE_DEPTH_GAUGE
from theater.harness.contracts.events import Event, EventKind
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlReceipt,
    ControlTransport,
    DeliveryResult,
    SessionOpenMode,
)
from theater.harness.source import Batch
from theater.models import Busy, JobState, NotYourChild, StaleTarget, now
from theater.observability import engine
from theater.observability.catalog import BY_KEY
from theater.observability.metrics import GaugeCache

CONTROL_DURATION = "theater.control.duration"
CONTROL_UNKNOWN = "theater.control.delivery.unknown"
RECONNECT_DURATION = "theater.runtime.reconnect.duration"
OBSERVATION_GAP = "theater.observation.gap"

#: The bounded delivery labels, transport labels, and counter reasons;
#: anything outside these sets in a recorded attribute would be a
#: cardinality or privacy regression.
DELIVERY_LABELS = {"accepted", "rejected", "unknown", "queued"}
TRANSPORT_LABELS = {"native_runtime", "legacy_tmux", "unknown"}
UNKNOWN_REASONS = {
    "ack_lost",
    "receipt_mismatch",
    "receipt_unknown",
    "uncorrelated",
    "readback_failed",
    "restart",
    "deadline",
}


class SpyBridge:
    """Records every metric the engine hands the process's one bridge."""

    active = True

    def __init__(self) -> None:
        self.records: list[tuple[str, float, dict]] = []
        self.counters: list[tuple[str, float, dict]] = []
        self.registered: list = []

    def register_specs(self, specs) -> None:
        self.registered.extend(specs)

    def record(self, name, value, attributes=None) -> None:
        self.records.append((name, value, dict(attributes or {})))

    def observe(self, spec, value, attributes=None) -> None:
        self.counters.append((spec.name, value, dict(attributes or {})))


class BoomBridge:
    """A bridge whose every method fails: instrumentation must fail open."""

    active = True

    def register_specs(self, specs) -> None:
        raise RuntimeError("registration broke")

    def record(self, name, value, attributes=None) -> None:
        raise RuntimeError("record broke")

    def observe(self, spec, value, attributes=None) -> None:
        raise RuntimeError("observe broke")


@pytest.fixture
def spy(monkeypatch) -> SpyBridge:
    bridge = SpyBridge()
    monkeypatch.setattr(engine, "_bridge", bridge)
    return bridge


@pytest.fixture
def boom(monkeypatch) -> BoomBridge:
    bridge = BoomBridge()
    monkeypatch.setattr(engine, "_bridge", bridge)
    return bridge


def durations(spy: SpyBridge, kind: str) -> list[dict]:
    """Latency records for one control kind, in emission order."""
    return [
        attrs
        for name, _value, attrs in spy.records
        if name == CONTROL_DURATION and attrs.get("kind") == kind
    ]


def unknown_counters(spy: SpyBridge) -> list[tuple[str, str]]:
    """The (kind, reason) pairs counted as unknown deliveries."""
    return [(attrs["kind"], attrs["reason"]) for _n, _v, attrs in spy.counters]


async def _harness_with(store, runtime_cls, participant_id: str = "p1") -> Harness:
    """One harness over a wrapped fake runtime sharing canned behavior."""
    base = make_runtime(participant_id)
    harness = Harness(store, {participant_id: wrap_runtime(base, runtime_cls)})
    await harness.runtimes[participant_id].open_session(mode=SessionOpenMode.NEW)
    return harness


# ---- control latency: every kind and outcome -------------------------------


async def test_send_latency_labels_accepted_and_rejected(store, spy) -> None:
    harness = await open_harness(store, "p1")

    job = await harness.service.send("p1", caller_id="caller", prompt="accepted")
    assert job.state == JobState.RUNNING
    assert durations(spy, "send") == [
        {
            "kind": "send",
            "delivery": "accepted",
            "transport": "native_runtime",
            "result": "success",
        }
    ]

    # A known-busy refusal raises unchanged: rejected/error, and the body
    # never established a transport, so the bounded default stays.
    with pytest.raises(Busy):
        await harness.service.send("p1", caller_id="caller", prompt="busy")
    assert durations(spy, "send")[-1] == {
        "kind": "send",
        "delivery": "rejected",
        "transport": "unknown",
        "result": "error",
    }


async def test_send_lost_ack_latency_is_unknown_and_counted(store, spy) -> None:
    """Ack lost: the honest latency label is unknown, never rejected."""

    class ExplodingRuntime(FakeRuntime):
        async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
            await super().send(operation_id=operation_id, prompt=prompt)
            raise ConnectionError("acknowledgement lost")

    harness = await _harness_with(store, ExplodingRuntime)
    job = await harness.service.send("p1", caller_id="caller", prompt="once only")
    assert job.state == JobState.RUNNING
    assert durations(spy, "send") == [
        {
            "kind": "send",
            "delivery": "unknown",
            "transport": "native_runtime",
            "result": "success",
        }
    ]
    assert unknown_counters(spy) == [("send", "ack_lost")]


async def test_steer_settings_interrupt_queue_latency_labels(store, spy) -> None:
    harness = await open_harness(store, "p1")

    # Steer with no active native turn: the refusal raises unchanged before
    # any transport fact exists, so the bounded default stays.
    with pytest.raises(StaleTarget):
        await harness.service.steer("p1", caller_id="caller", prompt="no turn")
    assert durations(spy, "steer") == [
        {
            "kind": "steer",
            "delivery": "rejected",
            "transport": "unknown",
            "result": "error",
        }
    ]

    # Settings applied and confirmed: accepted.
    outcome = await harness.service.update_settings("p1", caller_id="caller", model="m2")
    assert outcome.applied is True
    assert durations(spy, "settings_update") == [
        {
            "kind": "settings_update",
            "delivery": "accepted",
            "transport": "native_runtime",
            "result": "success",
        }
    ]

    # Interrupt while idle: already_idle, nothing interrupted.
    result = await harness.service.interrupt("p1", caller_id="caller")
    assert result.interrupted is False and result.reason == "already_idle"
    assert durations(spy, "interrupt") == [
        {
            "kind": "interrupt",
            "delivery": "rejected",
            "transport": "native_runtime",
            "result": "success",
        }
    ]

    # Queued followup: the queue accepted the item; delivery is observed later.
    state_of(harness, "p1").native_turn_id = "turn-keeps-queue-pending"
    job = await harness.service.queue_followup("p1", caller_id="caller", prompt="later")
    assert job.state == JobState.RUNNING  # awaitable now; delivery comes later
    assert durations(spy, "queue_followup") == [
        {
            "kind": "queue_followup",
            "delivery": "queued",
            "transport": "native_runtime",
            "result": "success",
        }
    ]

    # Every latency attribute set is bounded; the participant id is never one.
    for _name, _value, attrs in spy.records:
        assert attrs["delivery"] in DELIVERY_LABELS
        assert attrs["transport"] in TRANSPORT_LABELS
        assert "id" not in attrs
        assert "p1" not in attrs.values()


async def test_settings_readback_failure_labels_unknown(store, spy) -> None:
    """Accepted on the wire but never confirmed: unknown, never accepted."""

    class ReadbackExplodingRuntime(FakeRuntime):
        def __init__(self, context) -> None:
            super().__init__(context)
            self.explode_snapshot = False

        async def update_settings(
            self, *, operation_id, model=None, reasoning_effort=None
        ) -> ControlReceipt:
            receipt = await super().update_settings(
                operation_id=operation_id, model=model, reasoning_effort=reasoning_effort
            )
            self.explode_snapshot = True
            return receipt

        async def snapshot(self):
            if self.explode_snapshot:
                raise RuntimeError("effective-value readback failed")
            return await super().snapshot()

    harness = await _harness_with(store, ReadbackExplodingRuntime)
    outcome = await harness.service.update_settings("p1", caller_id="caller", model="m2")
    assert outcome.applied is None
    assert durations(spy, "settings_update") == [
        {
            "kind": "settings_update",
            "delivery": "unknown",
            "transport": "native_runtime",
            "result": "success",
        }
    ]
    assert ("settings_update", "readback_failed") in unknown_counters(spy)


# ---- unknown deliveries: bounded outcome/reason vocabulary -----------------


async def test_unknown_delivery_counter_reasons(store, spy) -> None:
    """Each uncertainty source carries its own fixed reason, never a retry."""

    class TurnlessRuntime(FakeRuntime):
        async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
            await super().send(operation_id=operation_id, prompt=prompt)
            return ControlReceipt(
                operation_id=operation_id,
                result=DeliveryResult.ACCEPTED,
                native_turn_id=None,
            )

    class UncertainRuntime(FakeRuntime):
        async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
            receipt = await super().send(operation_id=operation_id, prompt=prompt)
            return ControlReceipt(
                operation_id=operation_id,
                result=DeliveryResult.UNKNOWN,
                native_turn_id=receipt.native_turn_id,
            )

    class MismatchedReceiptRuntime(FakeRuntime):
        async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
            await super().send(operation_id=operation_id, prompt=prompt)
            return ControlReceipt(
                operation_id="some-other-operation",
                result=DeliveryResult.ACCEPTED,
                native_turn_id="turn-elsewhere",
            )

    for index, (runtime_cls, reason) in enumerate(
        (
            (TurnlessRuntime, "uncorrelated"),
            (UncertainRuntime, "receipt_unknown"),
            (MismatchedReceiptRuntime, "receipt_mismatch"),
        )
    ):
        # A fresh participant per case: a running job from one case must not
        # make the next participant's send look busy.
        pid = f"p{index + 1}"
        harness = await _harness_with(store, runtime_cls, pid)
        job = await harness.service.send(pid, caller_id="caller", prompt="uncertain")
        assert job.state == JobState.RUNNING  # never closed early, never retried
        assert unknown_counters(spy) == [("send", reason)]
        assert durations(spy, "send") == [
            {
                "kind": "send",
                "delivery": "unknown",
                "transport": "native_runtime",
                "result": "success",
            }
        ]
        spy.counters.clear()
        spy.records.clear()

    for _kind, reason in unknown_counters(spy):
        assert reason in UNKNOWN_REASONS


async def test_unknown_delivery_restart_and_deadline_reasons(store, spy) -> None:
    harness = await open_harness(store, "p1")

    # A jobless DISPATCHED row: transmission began, so a restart settles it
    # unknown with the restart reason — never a replay.
    store.reserve_control_operation(
        _make_operation(
            "p1#jobless:steer",
            job_handle=None,
            kind=ControlKind.STEER,
            transport=ControlTransport.NATIVE_RUNTIME,
            phase=ControlDeliveryPhase.RESERVED,
        )
    )
    store.mark_control_operation_dispatched("p1#jobless:steer", updated_at=now())

    harness.service.fail_undelivered_followups(["p1"])
    assert ("steer", "restart") in unknown_counters(spy)

    # The ambiguous-delivery deadline closes a never-confirmed send: deadline.
    class ExplodingRuntime(FakeRuntime):
        async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
            await super().send(operation_id=operation_id, prompt=prompt)
            raise ConnectionError("acknowledgement lost")

    harness = await _harness_with(store, ExplodingRuntime)
    job = await harness.service.send("p1", caller_id="caller", prompt="once")
    resolved = await harness.service.reconcile_ambiguous_delivery("p1", now_ts=now() + 31.0)
    assert [j.handle for j in resolved] == [job.handle]
    assert ("send", "deadline") in unknown_counters(spy)
    assert store.get_job(job.handle).error_code == "delivery_unknown"

    for _name, _value, attrs in spy.counters:
        assert set(attrs) == {"kind", "reason"}


async def test_control_service_registers_counter_on_the_one_bridge(store, spy) -> None:
    """Every service registers the same spec object on the process bridge."""
    from theater.daemon.controls import ControlGates, ControlService
    from theater.daemon.controls.service import _CONTROL_METRIC_SPECS
    from theater.daemon.jobs import JobManager

    async def noop(*args, **kwargs) -> None:
        return None

    gates = ControlGates(
        authorize=lambda *args: None,
        require_absent=noop,
        check_absent=lambda participant_id: None,
        send_preflight=noop,
        legacy_copy_mode_check=noop,
        legacy_busy_check=noop,
        check_prompt=lambda prompt: None,
        check_settings=lambda model, effort: None,
        cwd_for=lambda participant_id: "/tmp",
        legacy_deliver=noop,
    )
    jobs = JobManager(store)
    for _ in range(3):
        ControlService(store=store, jobs=jobs, runtime_for=lambda pid: None, gates=gates)
    assert all(spec is _CONTROL_METRIC_SPECS[0] for spec in spy.registered)
    assert spy.registered[0].name == CONTROL_UNKNOWN
    assert spy.registered[0].attribute_keys == ("kind", "reason")
    # The catalog stays one vocabulary: shared metric, metric-only gap.
    assert BY_KEY["CONTROL_SEND"].metric_name == CONTROL_DURATION
    assert BY_KEY["RUNTIME_RECONNECT"].metric_name == RECONNECT_DURATION
    assert BY_KEY["OBSERVATION_GAP"].metric_name == OBSERVATION_GAP
    assert BY_KEY["OBSERVATION_GAP"].record_outcome is False


# ---- failure isolation: a broken bridge never changes control behavior ------


async def test_broken_bridge_keeps_control_behavior(store, boom) -> None:
    """Every bridge method failing: sends still work and still refuse busy."""

    class ExplodingRuntime(FakeRuntime):
        async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
            await super().send(operation_id=operation_id, prompt=prompt)
            raise ConnectionError("acknowledgement lost")

    harness = await _harness_with(store, ExplodingRuntime)

    job = await harness.service.send("p1", caller_id="caller", prompt="uncertain")
    assert job.state == JobState.RUNNING
    with pytest.raises(Busy):
        await harness.service.send("p1", caller_id="caller", prompt="second")


async def test_failing_clock_setup_never_changes_the_control(store, spy, monkeypatch) -> None:
    """A clock that cannot be acquired disables measurement, never the send."""

    def broken_clock():
        raise RuntimeError("clock broke")

    monkeypatch.setattr("theater.daemon.controls.service.time.perf_counter", broken_clock)

    harness = await open_harness(store, "p1")

    job = await harness.service.send("p1", caller_id="caller", prompt="accepted")
    assert job.state == JobState.RUNNING  # the control itself is untouched
    assert durations(spy, "send") == []  # no duration could be measured

    # Exception behavior is exactly preserved: a busy target still refuses.
    with pytest.raises(Busy):
        await harness.service.send("p1", caller_id="caller", prompt="busy")


async def test_no_transport_classification_before_authorization(store, spy, monkeypatch) -> None:
    """No transport-classification read ever precedes the original order.

    Instrumentation must not add a store read before authorization, and a
    refusal raised before classification keeps the bounded ``unknown``
    transport label instead of paying for one.
    """
    from theater.daemon.controls import ControlService

    calls: list[str] = []
    original = ControlService._transport_for

    def recording(self, participant_id: str):
        calls.append(participant_id)
        return original(self, participant_id)

    monkeypatch.setattr(ControlService, "_transport_for", recording)

    # Unauthorized caller: the authorization refusal raises first and no
    # classification read happens anywhere on the path.
    harness = await open_harness(store, "p1")
    harness.gates_recorder.refuse_authorize_for = {"intruder"}
    with pytest.raises(NotYourChild):
        await harness.service.send("p1", caller_id="intruder", prompt="no")
    assert calls == []
    assert durations(spy, "send") == [
        {
            "kind": "send",
            "delivery": "rejected",
            "transport": "unknown",
            "result": "error",
        }
    ]

    # Disconnected native: fails closed with the original refusal and the
    # original order — no new read before or after authorization.
    detached = disconnected_native_harness(store, "p2")
    with pytest.raises(StaleTarget, match="natively wired"):
        await detached.service.send("p2", caller_id="caller", prompt="no fallback")
    assert calls == []

    # Nonexistent participant: exactly the original legacy flow, which never
    # classified a transport; the accepted legacy send labels its own fact.
    job = await harness.service.send("ghost", caller_id="caller", prompt="legacy")
    assert job.state == JobState.RUNNING
    assert calls == []
    assert durations(spy, "send")[-1] == {
        "kind": "send",
        "delivery": "accepted",
        "transport": "legacy_tmux",
        "result": "success",
    }

    # The body's own classification read still happens exactly where it
    # always did: reserving a queue slot (pre-existing, in-body).
    state_of(harness, "p1").native_turn_id = "turn-keeps-queue-pending"
    await harness.service.queue_followup("p1", caller_id="caller", prompt="later")
    assert calls == ["p1"]


async def test_broken_bridge_getter_never_changes_control(store, monkeypatch) -> None:
    """A metric_bridge() that itself raises changes nothing: fail open."""

    def broken_getter():
        raise RuntimeError("bridge getter broke")

    monkeypatch.setattr("theater.daemon.controls.service.metric_bridge", broken_getter)

    # Service construction registers specs through the getter: no raise.
    harness = await open_harness(store, "p1")
    job = await harness.service.send("p1", caller_id="caller", prompt="accepted")
    assert job.state == JobState.RUNNING

    # The unknown-delivery counter goes through the getter too: an
    # ack-lost send still returns its running job with no exception.
    class ExplodingRuntime(FakeRuntime):
        async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
            await super().send(operation_id=operation_id, prompt=prompt)
            raise ConnectionError("acknowledgement lost")

    detached = await _harness_with(store, ExplodingRuntime, "p2")
    uncertain = await detached.service.send("p2", caller_id="caller", prompt="uncertain")
    assert uncertain.state == JobState.RUNNING


# ---- runtime reconnects: span sources and generation checks -----------------


async def test_reconnect_span_records_source_and_outcome(monkeypatch) -> None:
    from tests.rig.fake_runtime import FakeRuntime, FakeRuntimeIO, FakeRuntimeState
    from theater.daemon.harness_runtime.errors import RuntimeGenerationMismatch
    from theater.daemon.harness_runtime.manager import HarnessRuntimeManager
    from theater.harness.contracts.runtime import RuntimeContext

    spy = SpyBridge()
    monkeypatch.setattr(engine, "_bridge", spy)

    def factory(participant_id: str, state: FakeRuntimeState):
        async def create() -> FakeRuntime:
            return FakeRuntime(
                RuntimeContext(
                    participant_id=participant_id,
                    cwd=None,
                    io=FakeRuntimeIO(state),
                    backend_generation=state.backend_generation,
                    endpoint=f"unix:///tmp/thtr-{participant_id}.sock",
                )
            )

        return create

    manager = HarnessRuntimeManager()
    state = FakeRuntimeState(participant_id="p1")
    first = await manager.get_or_create("p1", backend_generation=7, create=factory("p1", state))
    spy.records.clear()

    second = await manager.reconnect("p1", backend_generation=7, create=factory("p1", state))
    assert second is not first
    assert first.state.connected is False  # close-without-kill semantics intact
    assert len(spy.records) == 1
    name, value, attrs = spy.records[0]
    assert name == RECONNECT_DURATION and value >= 0.0
    assert attrs == {"source": "runtime_manager", "result": "success"}

    # A generation mismatch still fails closed and reads as an error outcome.
    spy.records.clear()
    with pytest.raises(RuntimeGenerationMismatch):
        await manager.reconnect("p1", backend_generation=8, create=factory("p1", state))
    name, _value, attrs = spy.records[-1]
    assert name == RECONNECT_DURATION
    assert attrs == {"source": "runtime_manager", "result": "error"}
    for _name, _value, attrs in spy.records:
        assert "id" not in attrs  # the participant id never becomes a label
    await manager.aclose()


# ---- queue depth: aggregate gauge source ------------------------------------


async def test_queue_depth_gauge_source_aggregates(store, registry, spy) -> None:
    """Pending followups across every participant, on the existing sampler."""
    from theater.daemon.runtime.lifecycle import _queued_followup_depth

    registry.register(harness="fake", pane=None, cwd="/tmp", claimed_id="p1")
    daemon = SimpleNamespace(store=store, registry=registry)
    assert _queued_followup_depth(daemon) == 0
    assert CONTROL_QUEUE_DEPTH_GAUGE in GaugeCache().names

    harness = await open_harness(store, "p1")
    state_of(harness, "p1").native_turn_id = "turn-keeps-queue-pending"
    await harness.service.queue_followup("p1", caller_id="caller", prompt="f0")
    await harness.service.queue_followup("p1", caller_id="caller", prompt="f1")
    depth = _queued_followup_depth(daemon)
    assert isinstance(depth, int)
    assert depth == 2


# ---- live observation gaps ----------------------------------------------------


async def test_observation_gap_measured_only_for_live_watches(store, registry, monkeypatch) -> None:
    """Gaps appear between live observations and reset on watch replacement."""
    spy = SpyBridge()
    monkeypatch.setattr(engine, "_bridge", spy)

    rig = Rig(store, registry, monkeypatch)
    registry.register(harness="fake", pane=None, cwd="/tmp", claimed_id="p1")
    try:
        await rig.open()
        await rig.warm_up()  # durable-only watch: registration is None

        def gaps() -> list[float]:
            return [value for name, value, _attrs in spy.records if name == OBSERVATION_GAP]

        def live_batch(text: str) -> None:
            rig.state.batches.append(Batch(events=(Event(kind=EventKind.ASSISTANT, text=text),)))

        assert gaps() == []  # nothing measured before live wiring

        rig.register_live()
        live_batch("live one")
        rig.observer.live.wake("p1")
        assert await until(lambda: "agent.assistant" in bus_kinds(rig.store))
        assert gaps() == []  # the first data-carrying read sets the reference

        live_batch("live two")
        rig.observer.live.wake("p1")
        assert await until(lambda: not rig.state.batches)
        assert await until(lambda: len(gaps()) >= 1)
        assert all(value >= 0.0 for value in gaps())
        for name, _value, attrs in spy.records:
            if name == OBSERVATION_GAP:
                assert attrs == {}  # no participant id, no labels at all
        gaps_before_replacement = len(gaps())

        # Replacement (a new registration is a new generation): the reference
        # resets, so the first observation after it measures no gap.
        rig.register_live()
        await asyncio.sleep(0.2)  # old watch cancelled, new watch started
        live_batch("live three")
        rig.observer.live.wake("p1")
        assert await until(lambda: not rig.state.batches)
        await asyncio.sleep(0.1)  # a carried-over reference would have fired
        assert len(gaps()) == gaps_before_replacement
    finally:
        await rig.aclose()
