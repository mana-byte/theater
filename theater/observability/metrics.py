"""Histogram registry, cached gauges, and GaugeSampler."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from theater.constants.observability import GAUGE_NAMES
from theater.observability.catalog import OperationSpec

logger = logging.getLogger("theater.observability.metrics")

CountSource = Callable[[], Any]


class MetricKind(StrEnum):
    HISTOGRAM = "histogram"
    COUNTER = "counter"


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """Describes one explicitly registered metric and its exact attribute schema."""

    name: str
    description: str
    unit: str
    kind: MetricKind
    attribute_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field in ("name", "description", "unit"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"metric spec {field} must be nonempty")
        if not isinstance(self.kind, MetricKind):
            raise TypeError("metric spec kind must be a MetricKind")
        keys = tuple(self.attribute_keys)
        if any(not isinstance(key, str) or not key.strip() for key in keys):
            raise ValueError("metric spec attribute keys must be nonempty")
        if len(set(keys)) != len(keys):
            raise ValueError("metric spec attribute keys must be unique")
        object.__setattr__(self, "attribute_keys", keys)


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
        for spec in specs:
            if spec.metric_name is None:
                continue
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


class CounterRegistry:
    """Builds and caches one monotonic counter per unique metric name."""

    def __init__(self, meter: Any | None = None) -> None:
        self._meter = meter
        self._entries: dict[str, _InstrumentEntry] = {}
        self._lock = threading.Lock()

    def get_or_create(self, name: str, description: str, unit: str) -> Any:
        with self._lock:
            entry = self._entries.get(name)
            if entry is not None:
                if entry.description != description:
                    raise ValueError(f"counter {name}: description mismatch")
                if entry.unit != unit:
                    raise ValueError(f"counter {name}: unit mismatch")
                return entry.instrument
            instrument = self._create_counter(name, description, unit)
            self._entries[name] = _InstrumentEntry(instrument, description, unit, "sum")
            return instrument

    def _create_counter(self, name: str, description: str, unit: str) -> Any:
        if self._meter is None:
            return None
        return self._meter.create_counter(name=name, description=description, unit=unit)

    def add(self, name: str, value: float, attributes: Mapping[str, Any] | None = None) -> None:
        if value < 0:
            raise ValueError(f"counter {name}: value must not be negative")
        with self._lock:
            entry = self._entries.get(name)
            if entry is None or entry.instrument is None:
                return
            instrument = entry.instrument
        with contextlib.suppress(Exception):
            instrument.add(value, attributes=dict(attributes) if attributes else None)


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

    def __init__(self, names: tuple[str, ...] = GAUGE_NAMES) -> None:
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
    """Thin bridge between the engine and registered metric instruments."""

    __slots__ = ("_active", "_counter_registry", "_gauge_cache", "_kinds", "_registry")

    def __init__(
        self,
        registry: HistogramRegistry | None = None,
        counter_registry: CounterRegistry | None = None,
    ) -> None:
        self._registry = registry
        self._counter_registry = counter_registry
        self._active = registry is not None or counter_registry is not None
        self._gauge_cache: GaugeCache | None = None
        self._kinds: dict[str, MetricKind] = {}

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

    def _register_kind(self, name: str, kind: MetricKind) -> None:
        existing = self._kinds.get(name)
        if existing is not None and existing is not kind:
            raise ValueError(f"metric {name}: kind mismatch ({existing.value} != {kind.value})")
        self._kinds[name] = kind

    def register_histogram(
        self, name: str, description: str, unit: str, aggregation: str = "exponential"
    ) -> None:
        self._register_kind(name, MetricKind.HISTOGRAM)
        if self._registry is not None:
            self._registry.get_or_create(name, description, unit, aggregation)

    def register_counter(self, name: str, description: str, unit: str) -> None:
        self._register_kind(name, MetricKind.COUNTER)
        if self._counter_registry is not None:
            self._counter_registry.get_or_create(name, description, unit)

    def register_specs(self, specs: tuple[MetricSpec, ...]) -> None:
        for spec in specs:
            if spec.kind is MetricKind.HISTOGRAM:
                self.register_histogram(spec.name, spec.description, spec.unit)
            else:
                self.register_counter(spec.name, spec.description, spec.unit)

    def observe(
        self, spec: MetricSpec, value: float, attributes: Mapping[str, Any] | None = None
    ) -> None:
        actual_keys = set(attributes) if attributes is not None else set()
        expected_keys = set(spec.attribute_keys)
        if actual_keys != expected_keys:
            raise ValueError(f"metric {spec.name}: attribute key set mismatch")
        if spec.kind is MetricKind.HISTOGRAM:
            self.record(spec.name, value, attributes)
        elif self._active and self._counter_registry is not None:
            self._counter_registry.add(spec.name, value, attributes)

    def deactivate(self) -> None:
        self._active = False
