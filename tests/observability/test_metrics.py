"""Metrics: registry, gauge cache and sampler, bridge API, store counts, gauge lifecycle."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from theater.daemon.jobs import JobManager
from theater.models import Job, JobState, Participant, Status, Tier, now
from theater.observability.catalog import OperationSpec
from theater.observability.engine import set_metric_bridge
from theater.observability.metrics import (
    CounterRegistry,
    GaugeCache,
    GaugeSampler,
    HistogramRegistry,
    MetricBridge,
    MetricKind,
    MetricSpec,
    _InstrumentEntry,
)


class _Meter:
    def __init__(self):
        self.histograms = []
        self.counters = []

    def create_histogram(self, **kwargs):
        instrument = _Instrument("record")
        self.histograms.append((kwargs, instrument))
        return instrument

    def create_counter(self, **kwargs):
        instrument = _Instrument("add")
        self.counters.append((kwargs, instrument))
        return instrument


class _Instrument:
    def __init__(self, method):
        self.calls = []
        self._method = method

    def record(self, value, attributes=None):
        assert self._method == "record"
        self.calls.append((value, attributes))

    def add(self, value, attributes=None):
        assert self._method == "add"
        self.calls.append((value, attributes))


@pytest.mark.parametrize(
    ("registry", "unit", "record"),
    [
        (HistogramRegistry, "ms", lambda reg: reg.record("m", 42.0)),
        (CounterRegistry, "1", lambda reg: reg.add("m", 1)),
    ],
    ids=["histogram", "counter"],
)
def test_a_registry_without_a_meter_is_a_noop(registry, unit, record):
    reg = registry(meter=None)
    reg.get_or_create("m", "d", unit)
    record(reg)


def test_registry_conflict():
    reg = HistogramRegistry(meter=None)
    reg.get_or_create("m", "d1", "ms")
    with pytest.raises(ValueError, match="description mismatch"):
        reg.get_or_create("m", "d2", "ms")


def test_catalog_registration_rejects_conflicting_duplicate():
    reg = HistogramRegistry(meter=None)
    specs = (
        OperationSpec("A", "a", None, "m", "first"),
        OperationSpec("B", "b", None, "m", "second"),
    )
    with pytest.raises(ValueError, match="description mismatch"):
        reg.register_from_catalog(specs)


def test_registry_lock_released_before_record():
    reg = HistogramRegistry(meter=None)

    class _Hist:
        def record(self, *a, **kw):
            assert not reg._lock.locked()

    reg._entries["m"] = _InstrumentEntry(_Hist(), "d", "ms", "exp")
    reg.record("m", 1.0)


def test_counter_registry_create_reuse_and_metadata_conflict():
    meter = _Meter()
    reg = CounterRegistry(meter)
    first = reg.get_or_create("m", "d", "1")
    assert reg.get_or_create("m", "d", "1") is first
    assert len(meter.counters) == 1
    with pytest.raises(ValueError, match="description mismatch"):
        reg.get_or_create("m", "other", "1")
    with pytest.raises(ValueError, match="unit mismatch"):
        reg.get_or_create("m", "d", "ms")


def test_counter_registry_add_rejects_negative_and_releases_lock():
    meter = _Meter()
    reg = CounterRegistry(meter)
    counter = reg.get_or_create("m", "d", "1")
    reg.add("m", 2, {"kind": "test"})
    assert counter.calls == [(2, {"kind": "test"})]
    with pytest.raises(ValueError, match="negative"):
        reg.add("m", -1)


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"name": "", "description": "d", "unit": "1", "kind": MetricKind.COUNTER}, "name"),
        ({"name": "m", "description": "", "unit": "1", "kind": MetricKind.COUNTER}, "description"),
        ({"name": "m", "description": "d", "unit": "", "kind": MetricKind.COUNTER}, "unit"),
        (
            {
                "name": "m",
                "description": "d",
                "unit": "1",
                "kind": MetricKind.COUNTER,
                "attribute_keys": ("x", "x"),
            },
            "unique",
        ),
    ],
)
def test_metric_spec_validation(kwargs, match):
    with pytest.raises(ValueError, match=match):
        MetricSpec(**kwargs)


def test_metric_spec_copies_attribute_keys():
    keys = ["kind"]
    spec = MetricSpec("m", "d", "1", MetricKind.COUNTER, keys)  # type: ignore[arg-type]
    keys.append("later")
    assert spec.attribute_keys == ("kind",)


def test_bridge_observe_requires_exact_schema_and_dispatches():
    meter = _Meter()
    bridge = MetricBridge(HistogramRegistry(meter), CounterRegistry(meter))
    histogram = MetricSpec("h", "histogram", "ms", MetricKind.HISTOGRAM, ("kind",))
    counter = MetricSpec("c", "counter", "1", MetricKind.COUNTER, ("kind",))
    bridge.register_specs((histogram, counter))
    bridge.observe(histogram, 3, {"kind": "x"})
    bridge.observe(counter, 4, {"kind": "x"})
    assert meter.histograms[0][1].calls == [(3, {"kind": "x"})]
    assert meter.counters[0][1].calls == [(4, {"kind": "x"})]
    with pytest.raises(ValueError, match="attribute key set mismatch"):
        bridge.observe(counter, 1, {})


def test_bridge_rejects_metric_name_kind_conflict():
    bridge = MetricBridge(HistogramRegistry(), CounterRegistry())
    bridge.register_histogram("m", "d", "ms")
    with pytest.raises(ValueError, match="kind mismatch"):
        bridge.register_counter("m", "d", "1")


def test_bridge_rejects_duplicate_schema_and_preserves_registered_spec():
    meter = _Meter()
    bridge = MetricBridge(HistogramRegistry(meter))
    registered = MetricSpec("m", "description", "ms", MetricKind.HISTOGRAM, ("kind",))
    conflicting = MetricSpec("m", "description", "ms", MetricKind.HISTOGRAM, ("other",))
    bridge.register_specs((registered,))
    with pytest.raises(ValueError, match="specification mismatch"):
        bridge.register_specs((conflicting,))
    bridge.observe(registered, 1, {"kind": "x"})
    assert meter.histograms[0][1].calls == [(1, {"kind": "x"})]


def test_bridge_rejects_unregistered_and_mismatched_observe_specs():
    bridge = MetricBridge(HistogramRegistry())
    registered = MetricSpec("m", "description", "ms", MetricKind.HISTOGRAM)
    equivalent = MetricSpec("m", "description", "ms", MetricKind.HISTOGRAM)
    other = MetricSpec("m", "other", "ms", MetricKind.HISTOGRAM)
    with pytest.raises(ValueError, match="not registered"):
        bridge.observe(registered, 1)
    bridge.register_specs((registered,))
    bridge.observe(equivalent, 1)
    with pytest.raises(ValueError, match="specification mismatch"):
        bridge.observe(other, 1)


def test_bridge_registry_failure_does_not_register_spec():
    registry = HistogramRegistry()
    registry.get_or_create("m", "existing", "ms")
    bridge = MetricBridge(registry)
    spec = MetricSpec("m", "new", "ms", MetricKind.HISTOGRAM)
    with pytest.raises(ValueError, match="description mismatch"):
        bridge.register_specs((spec,))
    with pytest.raises(ValueError, match="not registered"):
        bridge.observe(spec, 1)


def test_bridge_inactive_noop():
    MetricBridge(registry=None).record("any", 42.0)


def test_gauge_cache_thread_safe():
    g = GaugeCache()
    seen: list[int] = []
    g.set("theater.participants.live", 0)

    def read() -> None:
        for _ in range(1000):
            value = g.get("theater.participants.live")
            if value is not None:
                seen.append(value)

    t = threading.Thread(target=read)
    t.start()
    for i in range(1000):
        g.set("theater.participants.live", i)
    t.join()
    assert all(isinstance(v, int) for v in seen)


def test_gauge_cache_unknown_raises():
    with pytest.raises(KeyError):
        GaugeCache().set("bogus", 1)


def test_callback_empty_before_sample():
    assert GaugeCache().make_callbacks()["theater.participants.live"](None) == []


def test_callback_returns_observation():
    g = GaugeCache()
    g.set("theater.participants.live", 5)
    result = g.make_callbacks()["theater.participants.live"](None)
    assert len(result) == 1 and result[0].value == 5


def test_sampler_initial_sample_done():
    async def run():
        s = GaugeSampler(10.0, sources={"theater.participants.live": lambda: 42})
        await s.start()
        assert s.cache.get("theater.participants.live") == 42
        await s.stop()

    asyncio.run(run())


def test_sampler_rejects_unknown_source():
    with pytest.raises(KeyError):
        GaugeSampler(10.0).add_source("bogus", lambda: 1)


def test_sampler_no_post_stop_access():
    calls = []

    async def run():
        s = GaugeSampler(0.01, sources={"theater.participants.live": lambda: calls.append(1) or 42})
        await s.start()
        await asyncio.sleep(0.05)
        await s.stop()
        count = len(calls)
        await asyncio.sleep(0.05)
        assert len(calls) == count

    asyncio.run(run())


def test_sampler_isawaitable_for_future():
    async def run():
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(7)
        s = GaugeSampler(10.0, sources={"theater.participants.live": lambda: fut})
        await s.start()
        assert s.cache.get("theater.participants.live") == 7
        await s.stop()

    asyncio.run(run())


def test_create_active_gauge_sampler_uses_bridge_cache(monkeypatch):
    from theater.observability.engine import set_metric_bridge

    reg = HistogramRegistry(meter=None)
    bridge = MetricBridge(reg)
    cache = GaugeCache()
    bridge.set_gauge_cache(cache)
    set_metric_bridge(bridge)
    try:
        from theater.observability.metrics import create_active_gauge_sampler

        sampler = create_active_gauge_sampler(1.0, {"theater.participants.live": lambda: 3})
        assert sampler is not None and sampler.cache is cache
    finally:
        set_metric_bridge(None)


def test_create_active_none_no_bridge():
    from theater.observability.metrics import create_active_gauge_sampler

    assert create_active_gauge_sampler(1.0, {}) is None


def test_participant_counts_match_domain_semantics(store, registry):
    spawned = Participant(harness="vibe", tier=Tier.SPAWNED, tmux_pane="%1", cwd="/tmp")
    adopted = Participant(harness="vibe", tier=Tier.ADOPTED, tmux_pane="%2", cwd="/tmp")
    external_with_pane = Participant(harness="vibe", tier=Tier.EXTERNAL, tmux_pane="%3", cwd="/tmp")
    spawned_no_pane = Participant(harness="vibe", tier=Tier.SPAWNED, cwd="/tmp")
    dead_spawned = Participant(harness="vibe", tier=Tier.SPAWNED, cwd="/tmp")
    for p in (spawned, adopted, external_with_pane, spawned_no_pane, dead_spawned):
        store.upsert_participant(p)
    store.set_status(dead_spawned.id, Status.DEAD)

    all_rows = store.list_participants(include_dead=True)
    assert store.live_count() == sum(1 for p in all_rows if p.status is not Status.DEAD)
    assert store.addressable_count() == sum(1 for p in all_rows if p.addressable)
    assert store.live_count() == 4
    assert store.addressable_count() == 0
    assert registry.live_count() == store.live_count()
    registry.configure_addressability(lambda participant_id: participant_id == spawned.id)
    assert registry.addressable_count() == 1


def test_job_active_count_covers_all_states_and_forwards(store):
    for handle, state in (
        ("r1", JobState.RUNNING),
        ("r2", JobState.RUNNING),
        ("d1", JobState.DONE),
        ("c1", JobState.CRASHED),
        ("k1", JobState.KILLED),
    ):
        store.create_job(
            Job(
                handle=handle,
                caller_id="cli",
                target_id="t",
                kind="send",
                prompt=None,
                state=state,
                result=None,
                error_code=None,
                created_at=now(),
                finished_at=now() if state is not JobState.RUNNING else None,
            )
        )
    assert store.active_job_count() == 2

    jm = JobManager(store)
    assert jm.active_count() == 2
    jm.finish("r1", state=JobState.DONE, result="ok")
    assert jm.active_count() == 1
    jm.finish("r2", state=JobState.KILLED, error_code="killed")
    assert jm.active_count() == 0
    assert store.active_job_count() == 0


def test_counts_zero_on_empty_store(store):
    assert store.live_count() == 0
    assert store.addressable_count() == 0
    assert store.active_job_count() == 0


def _fake_daemon(*, with_sampler=False):
    daemon = MagicMock()
    daemon.config.observability.gauge_interval_s = 1.0
    daemon.registry.live_count = MagicMock(return_value=3)
    daemon.registry.addressable_count = MagicMock(return_value=2)
    daemon.jobs.active_count = MagicMock(return_value=1)
    daemon._gauge_sampler = MagicMock() if with_sampler else None
    return daemon


async def test_gauge_sampler_absent_bridge_creates_nothing():
    from theater.daemon.runtime.lifecycle import _start_gauge_sampler

    set_metric_bridge(None)
    try:
        daemon = _fake_daemon()
        await _start_gauge_sampler(daemon)
    finally:
        set_metric_bridge(None)

    assert not hasattr(daemon, "_gauge_sampler") or daemon._gauge_sampler is None
    daemon.registry.live_count.assert_not_called()
    daemon.registry.addressable_count.assert_not_called()
    daemon.jobs.active_count.assert_not_called()


async def test_gauge_sampler_starts_after_reconcile_with_active_bridge():
    from theater.daemon.runtime.lifecycle import _start_gauge_sampler

    reg = HistogramRegistry(meter=None)
    bridge = MetricBridge(reg)
    cache = GaugeCache()
    bridge.set_gauge_cache(cache)
    set_metric_bridge(bridge)
    try:
        daemon = _fake_daemon()
        await _start_gauge_sampler(daemon)
        assert daemon._gauge_sampler is not None
        assert cache.get("theater.participants.live") == 3
        assert cache.get("theater.participants.addressable") == 2
        assert cache.get("theater.jobs.active") == 1
        await daemon._gauge_sampler.stop()
    finally:
        set_metric_bridge(None)


async def test_aclose_stops_sampler_before_store_close():
    from theater.daemon.runtime.lifecycle import aclose

    calls: list[str] = []

    store_close = MagicMock(side_effect=lambda: calls.append("store"))
    sampler_stop = AsyncMock(side_effect=lambda: calls.append("sampler"))
    controls_close = AsyncMock(side_effect=lambda: calls.append("controls"))
    observer_close = AsyncMock(side_effect=lambda: calls.append("observer"))
    runtime_recovery_stop = AsyncMock(side_effect=lambda: calls.append("runtime_recovery"))
    runtime_manager_close = AsyncMock(side_effect=lambda: calls.append("runtime_manager"))
    daemon = MagicMock()
    daemon._server = None
    daemon._reaper = None
    daemon._gc = None
    daemon._lag = None
    daemon._conns = set()
    daemon.store.close = store_close
    daemon._gauge_sampler = MagicMock()
    daemon._gauge_sampler.stop = sampler_stop
    daemon._release_files = MagicMock()
    daemon.controls = MagicMock()
    daemon.controls.aclose = controls_close
    daemon.observer = MagicMock()
    daemon.observer.aclose = observer_close
    daemon.runtime_manager = MagicMock()
    daemon.runtime_manager.stop_recovery = runtime_recovery_stop
    daemon.runtime_manager.aclose = runtime_manager_close
    daemon.otel_runtime = MagicMock()
    daemon.otel_runtime.aclose = AsyncMock()
    daemon.hook_runtime = MagicMock()
    daemon.hook_runtime.aclose = AsyncMock()

    await aclose(daemon, close_timeout=1.0, shutdown_workers=AsyncMock())

    assert calls == [
        "runtime_recovery",
        "controls",
        "observer",
        "runtime_manager",
        "sampler",
        "store",
    ]
    assert daemon._gauge_sampler is None
    daemon.controls.aclose.assert_awaited_once_with()
    daemon.observer.aclose.assert_awaited_once_with()
    daemon.runtime_manager.stop_recovery.assert_awaited_once_with()
    daemon.runtime_manager.aclose.assert_awaited_once_with()
    daemon.otel_runtime.aclose.assert_awaited_once_with()
    daemon.hook_runtime.aclose.assert_awaited_once_with()
