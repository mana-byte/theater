"""Histogram registry, cached gauges, and GaugeSampler."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import threading
from collections.abc import Callable, Mapping
from typing import Any

from theater.observability.catalog import OperationSpec

logger = logging.getLogger("theater.observability.metrics")

CountSource = Callable[[], Any]

_GAUGE_NAMES = (
    "theater.participants.live",
    "theater.participants.addressable",
    "theater.jobs.active",
)


class _InstrumentEntry:
    __slots__ = ("aggregation", "description", "instrument", "unit")

    def __init__(self, instrument: Any, description: str, unit: str, aggregation: str) -> None:
        self.instrument = instrument
        self.description = description
        self.unit = unit
        self.aggregation = aggregation


class HistogramRegistry:
    """Builds and caches one histogram per unique catalog metric name."""

    def __init__(self, meter: Any | None = None) -> None:
        self._meter = meter
        self._entries: dict[str, _InstrumentEntry] = {}
        self._lock = threading.Lock()

    def register_from_catalog(self, specs: tuple[OperationSpec, ...]) -> None:
        seen: set[str] = set()
        for spec in specs:
            if spec.metric_name is None or spec.metric_name in seen:
                continue
            seen.add(spec.metric_name)
            self.get_or_create(spec.metric_name, spec.description or "", spec.unit, "exponential")

    def get_or_create(
        self, name: str, description: str, unit: str, aggregation: str = "exponential"
    ) -> Any:
        with self._lock:
            entry = self._entries.get(name)
            if entry is not None:
                if entry.description != description:
                    raise ValueError(f"histogram {name}: description mismatch")
                if entry.unit != unit:
                    raise ValueError(f"histogram {name}: unit mismatch")
                if entry.aggregation != aggregation:
                    raise ValueError(f"histogram {name}: aggregation mismatch")
                return entry.instrument
            instrument = self._create_histogram(name, description, unit)
            self._entries[name] = _InstrumentEntry(instrument, description, unit, aggregation)
            return instrument

    def _create_histogram(self, name: str, description: str, unit: str) -> Any:
        if self._meter is None:
            return None
        return self._meter.create_histogram(name=name, description=description, unit=unit)

    def record(self, name: str, value: float, attributes: Mapping[str, Any] | None = None) -> None:
        with self._lock:
            entry = self._entries.get(name)
            if entry is None or entry.instrument is None:
                return
            instrument = entry.instrument
        with contextlib.suppress(Exception):
            instrument.record(value, attributes=dict(attributes) if attributes else None)


class _CachedGauge:
    __slots__ = ("_has_value", "_lock", "_value")

    def __init__(self) -> None:
        self._value: int = 0
        self._lock = threading.Lock()
        self._has_value = False

    def set(self, value: int) -> None:
        with self._lock:
            self._value = value
            self._has_value = True

    def get(self) -> int | None:
        with self._lock:
            return self._value if self._has_value else None


class GaugeCache:
    """Bridge-owned thread-safe cache for observable gauge callbacks."""

    __slots__ = ("_caches",)

    def __init__(self, names: tuple[str, ...] = _GAUGE_NAMES) -> None:
        self._caches: dict[str, _CachedGauge] = {name: _CachedGauge() for name in names}

    def set(self, name: str, value: int) -> None:
        cache = self._caches.get(name)
        if cache is None:
            raise KeyError(f"unknown gauge: {name}")
        cache.set(value)

    def get(self, name: str) -> int | None:
        cache = self._caches.get(name)
        return cache.get() if cache is not None else None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._caches)

    def make_callbacks(self) -> dict[str, Callable[[Any], list[Any]]]:
        result: dict[str, Callable[[Any], list[Any]]] = {}
        for name, cache in self._caches.items():

            def _make_cb(c: _CachedGauge) -> Callable[[Any], list[Any]]:
                def _cb(options: Any) -> list[Any]:
                    from opentelemetry.metrics import Observation

                    val = c.get()
                    if val is None:
                        return []
                    return [Observation(val)]

                return _cb

            result[name] = _make_cb(cache)
        return result

    def register_observable_gauges(self, meter: Any) -> None:
        for name, cb in self.make_callbacks().items():
            meter.create_observable_gauge(name=name, callbacks=[cb])


class GaugeSampler:
    """Periodically samples count sources into a shared GaugeCache."""

    def __init__(
        self,
        interval_s: float,
        sources: Mapping[str, CountSource] | None = None,
        cache: GaugeCache | None = None,
    ) -> None:
        self._interval_s = interval_s
        self._cache = cache or GaugeCache()
        self._sources: dict[str, CountSource] = {}
        if sources:
            for name, src in sources.items():
                self.add_source(name, src)
        self._task: asyncio.Task[None] | None = None

    @property
    def cache(self) -> GaugeCache:
        return self._cache

    def add_source(self, name: str, source: CountSource) -> None:
        if name not in self._cache.names:
            raise KeyError(f"unknown gauge: {name}")
        self._sources[name] = source

    async def _read_source(self, source: CountSource) -> int:
        result = source()
        if inspect.isawaitable(result):
            result = await result
        return int(result)

    async def _sample_all(self) -> None:
        for name, source in self._sources.items():
            try:
                value = await self._read_source(source)
                self._cache.set(name, value)
            except Exception:
                logger.debug("gauge %s: read failed", name, exc_info=True)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_s)
            await self._sample_all()

    async def start(self) -> None:
        if self._task is not None:
            return
        await self._sample_all()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


def create_active_gauge_sampler(
    interval_s: float, sources: Mapping[str, CountSource]
) -> GaugeSampler | None:
    """Create a GaugeSampler using the active runtime bridge's gauge cache.

    Returns None when no active bridge exists or bridge has no gauge cache.
    The sampler writes to the same cache that runtime-registered callbacks read.
    """
    from theater.observability.engine import metric_bridge

    bridge = metric_bridge()
    if bridge is None or not bridge.active or bridge.gauge_cache is None:
        return None
    return GaugeSampler(interval_s, sources, cache=bridge.gauge_cache)


class MetricBridge:
    """Thin bridge between the engine and the histogram registry."""

    __slots__ = ("_active", "_gauge_cache", "_registry")

    def __init__(self, registry: HistogramRegistry | None = None) -> None:
        self._registry = registry
        self._active = registry is not None
        self._gauge_cache: GaugeCache | None = None

    @property
    def active(self) -> bool:
        return self._active

    @property
    def gauge_cache(self) -> GaugeCache | None:
        return self._gauge_cache

    def set_gauge_cache(self, cache: GaugeCache | None) -> None:
        self._gauge_cache = cache

    def record(
        self, metric_name: str, value: float, attributes: Mapping[str, Any] | None = None
    ) -> None:
        if not self._active or self._registry is None:
            return
        self._registry.record(metric_name, value, attributes)

    def deactivate(self) -> None:
        self._active = False
