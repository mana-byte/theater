"""Regression tests for observability phase 2C call-site migration."""

from __future__ import annotations

import contextvars
from unittest.mock import AsyncMock, MagicMock

from theater.observability.engine import set_metric_bridge
from theater.observability.metrics import GaugeCache, HistogramRegistry, MetricBridge

TIMING = "theater.timing"


# --- workers context propagation -------------------------------------


async def test_to_thread_propagates_contextvar():
    """copy_context lets a ContextVar set on the loop survive into the worker."""
    from theater.daemon import workers

    marker: contextvars.ContextVar[str | None] = contextvars.ContextVar("marker", default=None)
    token = marker.set("from-loop")
    seen: list[str | None] = []

    def _read_marker() -> str | None:
        seen.append(marker.get())
        return marker.get()

    try:
        result = await workers.to_thread(_read_marker, label="ctx-test")
    finally:
        marker.reset(token)
        await workers.shutdown()

    assert result == "from-loop"
    assert seen == ["from-loop"]


# --- tmux run synthetic error + check ---------------------------------


def _make_asyncio_proc(returncode: int, stdout: bytes, stderr: bytes = b""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    return proc


# --- lifecycle gauge sampler ------------------------------------------


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
