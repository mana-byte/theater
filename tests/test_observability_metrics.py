"""Metrics: registry, thread cache, GaugeSampler, callbacks, bridge API."""

from __future__ import annotations

import asyncio
import threading

import pytest

from theater.observability.catalog import OperationSpec
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


def test_registry_no_meter_noop():
    reg = HistogramRegistry(meter=None)
    reg.get_or_create("m", "d", "ms")
    reg.record("m", 42.0)


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


def test_counter_registry_add_rejects_negative_and_releases_lock():
    meter = _Meter()
    reg = CounterRegistry(meter)
    counter = reg.get_or_create("m", "d", "1")
    reg.add("m", 2, {"kind": "test"})
    assert counter.calls == [(2, {"kind": "test"})]
    with pytest.raises(ValueError, match="negative"):
        reg.add("m", -1)


def test_counter_registry_no_meter_noop():
    reg = CounterRegistry(meter=None)
    reg.get_or_create("m", "d", "1")
    reg.add("m", 1)


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
