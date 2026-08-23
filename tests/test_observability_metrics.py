"""Metrics: registry, thread cache, GaugeSampler, callbacks, bridge API."""

from __future__ import annotations

import asyncio
import threading

import pytest

from theater.observability.metrics import (
    GaugeCache,
    GaugeSampler,
    HistogramRegistry,
    MetricBridge,
    _InstrumentEntry,
)


def test_registry_no_meter_noop():
    reg = HistogramRegistry(meter=None)
    reg.get_or_create("m", "d", "ms")
    reg.record("m", 42.0)


def test_registry_conflict():
    reg = HistogramRegistry(meter=None)
    reg.get_or_create("m", "d1", "ms")
    with pytest.raises(ValueError, match="description mismatch"):
        reg.get_or_create("m", "d2", "ms")


def test_registry_lock_released_before_record():
    reg = HistogramRegistry(meter=None)

    class _Hist:
        def record(self, *a, **kw):
            assert not reg._lock.locked()

    reg._entries["m"] = _InstrumentEntry(_Hist(), "d", "ms", "exp")
    reg.record("m", 1.0)


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
