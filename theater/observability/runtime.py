"""Process-level composition and RuntimeHandle shutdown."""

from __future__ import annotations

import contextlib
import logging
import threading
from pathlib import Path
from typing import Any

from theater.constants.observability import (
    BATCH_QUEUE_SIZE,
    DEFAULT_EXPORT_INTERVAL_MS,
    DEFAULT_LOG_BACKUP_COUNT,
    DEFAULT_LOG_MAX_BYTES,
    DEFAULT_OTLP_PROTOCOL,
    DEFAULT_SERVICE_NAME,
    DERIVED_GRPC_ENDPOINT,
    DERIVED_HTTP_ENDPOINT,
    EXPORT_BATCH_SIZE,
    EXPORT_TIMEOUT_S,
    HISTOGRAM_MAX_SCALE,
    HISTOGRAM_MAX_SIZE,
    OTLP_PROTOCOL_GRPC,
    OTLP_PROTOCOLS,
    PROCESS_ROLE_DAEMON,
    PROCESS_ROLES,
)
from theater.observability.metrics import MetricKind, MetricSpec

logger = logging.getLogger("theater.observability.runtime")

_configured = False
_guard_lock = threading.Lock()


class ObservabilityError(Exception):
    pass


def _check_otel_available() -> None:
    try:
        import opentelemetry.exporter.otlp.proto.grpc
        import opentelemetry.exporter.otlp.proto.http
        import opentelemetry.sdk  # noqa: F401
    except ImportError as exc:
        logger.error(  # noqa: TRY400
            "OpenTelemetry SDK or exporters not found: "
            "install the observability extra or disable observability.otlp_enabled"
        )
        raise ObservabilityError(
            "OpenTelemetry SDK or exporters not found: "
            "install the observability extra or disable observability.otlp_enabled"
        ) from exc


def _validate_endpoint(endpoint: str) -> None:
    from urllib.parse import urlparse

    try:
        parsed = urlparse(endpoint)
        hostname = parsed.hostname
    except ValueError as exc:
        raise ObservabilityError(f"invalid otlp_endpoint {endpoint!r}: {exc}") from exc
    if parsed.scheme not in ("http", "https"):
        raise ObservabilityError(f"otlp_endpoint must be http or https, got {parsed.scheme!r}")
    if not hostname:
        raise ObservabilityError(f"otlp_endpoint must have a host, got {endpoint!r}")
    if parsed.query or parsed.fragment:
        raise ObservabilityError(f"otlp_endpoint must not have query/fragment, got {endpoint!r}")


def _resolve_endpoints(protocol: str, configured: str | None) -> tuple[str, str, str]:
    if configured is None:
        base = DERIVED_GRPC_ENDPOINT if protocol == OTLP_PROTOCOL_GRPC else DERIVED_HTTP_ENDPOINT
    else:
        base = configured
        _validate_endpoint(base)
    if protocol == OTLP_PROTOCOL_GRPC:
        return base, base, base
    base = base.rstrip("/")
    return f"{base}/v1/traces", f"{base}/v1/metrics", f"{base}/v1/logs"


def _get_version() -> str:
    try:
        from importlib.metadata import version

        return version("theater")
    except Exception:
        return "unknown"


class _HandlerEntry:
    __slots__ = ("attachments", "handler", "is_otel")

    def __init__(self, handler: logging.Handler, is_otel: bool = False) -> None:
        self.handler = handler
        self.attachments: list[str] = []
        self.is_otel = is_otel


class _ExcludeOtelRecursion(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not (
            record.name == "opentelemetry"
            or record.name.startswith("opentelemetry.")
            or record.name == "theater.observability"
            or record.name.startswith("theater.observability.")
        )


def _shutdown_provider(provider: Any, timeout_ms: int) -> None:
    if provider is None:
        return

    def _close() -> None:
        with contextlib.suppress(Exception):
            if hasattr(provider, "force_flush"):
                provider.force_flush(timeout_millis=timeout_ms)
        with contextlib.suppress(Exception):
            if hasattr(provider, "shutdown"):
                try:
                    provider.shutdown(timeout_millis=timeout_ms)
                except TypeError:
                    provider.shutdown()

    worker = threading.Thread(target=_close, name="theater-otel-shutdown", daemon=True)
    worker.start()
    worker.join(timeout_ms / 1000.0)
    if worker.is_alive():
        with contextlib.suppress(Exception):
            logger.warning("OpenTelemetry shutdown exceeded %.1fs", timeout_ms / 1000.0)


def _close_handlers(entries: list[_HandlerEntry], *, file_handlers: bool) -> None:
    for entry in entries:
        if isinstance(entry.handler, logging.FileHandler) is not file_handlers:
            continue
        with contextlib.suppress(Exception):
            for name in entry.attachments:
                logging.getLogger(name).removeHandler(entry.handler)
        with contextlib.suppress(Exception):
            entry.handler.close()


class RuntimeHandle:
    __slots__ = (
        "_closed",
        "_handler_entries",
        "_lock",
        "_logger_provider",
        "_logger_states",
        "_meter_provider",
        "_metric_bridge",
        "_otel_entry",
        "_tracer_provider",
    )

    def __init__(self) -> None:
        self._closed = False
        self._lock = threading.Lock()
        self._handler_entries: list[_HandlerEntry] = []
        self._logger_states: dict[str, tuple[int, bool]] = {}
        self._meter_provider: Any = None
        self._tracer_provider: Any = None
        self._logger_provider: Any = None
        self._metric_bridge: Any = None
        self._otel_entry: _HandlerEntry | None = None

    def add_handler(
        self, handler: logging.Handler, *names: str, is_otel: bool = False
    ) -> _HandlerEntry:
        entry = _HandlerEntry(handler, is_otel)
        for name in names:
            target = logging.getLogger(name)
            self._logger_states.setdefault(name, (target.level, target.propagate))
            target.addHandler(handler)
            entry.attachments.append(name)
        self._handler_entries.append(entry)
        if is_otel:
            self._otel_entry = entry
        return entry

    def share_handler(self, entry: _HandlerEntry, *names: str) -> None:
        for name in names:
            target = logging.getLogger(name)
            self._logger_states.setdefault(name, (target.level, target.propagate))
            target.addHandler(entry.handler)
            entry.attachments.append(name)

    def set_logger_level(self, name: str, level: int) -> None:
        target = logging.getLogger(name)
        self._logger_states.setdefault(name, (target.level, target.propagate))
        target.setLevel(level)

    def set_logger_propagate(self, name: str, propagate: bool) -> None:
        target = logging.getLogger(name)
        self._logger_states.setdefault(name, (target.level, target.propagate))
        target.propagate = propagate

    @property
    def closed(self) -> bool:
        return self._closed

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        from theater.observability.engine import set_metric_bridge

        with contextlib.suppress(Exception):
            set_metric_bridge(None)
        if self._metric_bridge is not None:
            with contextlib.suppress(Exception):
                self._metric_bridge.deactivate()
        if self._otel_entry is not None:
            with contextlib.suppress(Exception):
                for name in self._otel_entry.attachments:
                    logging.getLogger(name).removeHandler(self._otel_entry.handler)
        timeout_ms = int(EXPORT_TIMEOUT_S * 1000)
        _shutdown_provider(self._logger_provider, timeout_ms)
        _shutdown_provider(self._meter_provider, timeout_ms)
        _shutdown_provider(self._tracer_provider, timeout_ms)
        _close_handlers(self._handler_entries, file_handlers=False)
        for name, (level, propagate) in self._logger_states.items():
            with contextlib.suppress(Exception):
                target = logging.getLogger(name)
                target.setLevel(level)
                target.propagate = propagate
        _close_handlers(self._handler_entries, file_handlers=True)


def _validate_params(
    role: str,
    protocol: str,
    service_name: str,
    export_interval_ms: int,
    log_max_bytes: int,
    log_backup_count: int,
    log_level: str,
) -> int:
    if role not in PROCESS_ROLES:
        raise ObservabilityError(f"role must be one of {sorted(PROCESS_ROLES)}, got {role!r}")
    if protocol not in OTLP_PROTOCOLS:
        raise ObservabilityError(
            f"otlp_protocol must be one of {sorted(OTLP_PROTOCOLS)}, got {protocol!r}"
        )
    if not service_name.strip():
        raise ObservabilityError("service_name must not be blank")
    if export_interval_ms <= 0:
        raise ObservabilityError("export_interval_ms must be positive")
    if log_max_bytes <= 0:
        raise ObservabilityError("log_max_bytes must be positive")
    if log_backup_count <= 0:
        raise ObservabilityError("log_backup_count must be positive")
    level = getattr(logging, log_level.upper(), None)
    if not isinstance(level, int):
        raise ObservabilityError(f"invalid log_level: {log_level!r}")
    return level


def configure(
    *,
    role: str,
    otlp_enabled: bool = False,
    otlp_protocol: str = DEFAULT_OTLP_PROTOCOL,
    otlp_endpoint: str | None = None,
    service_name: str = DEFAULT_SERVICE_NAME,
    export_interval_ms: int = DEFAULT_EXPORT_INTERVAL_MS,
    log_level: str = "INFO",
    log_max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    log_backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
    log_path: Path | None = None,
    foreground: bool = False,
    metric_specs: tuple[MetricSpec, ...] = (),
) -> RuntimeHandle:
    """Configure process-level observability exactly once."""
    global _configured  # noqa: PLW0603

    level = _validate_params(
        role,
        otlp_protocol,
        service_name,
        export_interval_ms,
        log_max_bytes,
        log_backup_count,
        log_level,
    )
    if otlp_endpoint is not None:
        _validate_endpoint(otlp_endpoint)

    with _guard_lock:
        if _configured:
            raise ObservabilityError("configure() already called; OTel globals are one-shot")
        handle = RuntimeHandle()
        file_entry: _HandlerEntry | None = None
        try:
            if log_path is not None:
                from theater.observability.logging import make_rotating_handler, make_stderr_handler

                file_entry = handle.add_handler(
                    make_rotating_handler(log_path, log_max_bytes, log_backup_count), "theater"
                )
                handle.set_logger_level("theater", level)
                handle.set_logger_propagate("theater", False)
                if foreground:
                    handle.add_handler(make_stderr_handler(), "theater")

            if otlp_enabled:
                _check_otel_available()
                _stage_otel(
                    handle,
                    role,
                    otlp_protocol,
                    otlp_endpoint,
                    service_name,
                    export_interval_ms,
                    level,
                    file_entry,
                    metric_specs,
                )
        except Exception:
            handle.shutdown()
            raise
        _configured = True
        return handle


def _check_existing_provider() -> None:
    from opentelemetry.trace import ProxyTracerProvider, get_tracer_provider

    current = get_tracer_provider()
    if not isinstance(current, ProxyTracerProvider):
        raise ObservabilityError(f"existing real tracer provider (type={type(current).__name__})")


def _build_exporter(cls: type, endpoint: str) -> Any:
    """Build a single exporter; timeout unit is seconds."""
    return cls(endpoint=endpoint, timeout=EXPORT_TIMEOUT_S)


def _build_exporters(
    protocol: str, endpoints: tuple[str, str, str], staged: _StagedResources
) -> tuple[Any, Any, Any]:
    traces, metrics, logs = endpoints
    if protocol == OTLP_PROTOCOL_GRPC:
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    else:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import (  # type: ignore[assignment]
            OTLPLogExporter,
        )
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (  # type: ignore[assignment]
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[assignment]
            OTLPSpanExporter,
        )
    trace_exporter = _build_exporter(OTLPSpanExporter, traces)
    staged.add(trace_exporter)
    metric_exporter = _build_exporter(OTLPMetricExporter, metrics)
    staged.add(metric_exporter)
    log_exporter = _build_exporter(OTLPLogExporter, logs)
    staged.add(log_exporter)
    return trace_exporter, metric_exporter, log_exporter


def _build_views(metric_specs: tuple[MetricSpec, ...] = ()) -> list[Any]:
    from opentelemetry.sdk.metrics.view import ExponentialBucketHistogramAggregation, View

    from theater.observability.catalog import OPERATIONS

    views: list[Any] = []
    seen: set[str] = set()
    for operation in OPERATIONS:
        if operation.metric_name is None or operation.metric_name in seen:
            continue
        seen.add(operation.metric_name)
        views.append(
            View(
                instrument_name=operation.metric_name,
                aggregation=ExponentialBucketHistogramAggregation(
                    max_size=HISTOGRAM_MAX_SIZE, max_scale=HISTOGRAM_MAX_SCALE
                ),
            )
        )
    for spec in metric_specs:
        if spec.kind is not MetricKind.HISTOGRAM or spec.name in seen:
            continue
        seen.add(spec.name)
        views.append(
            View(
                instrument_name=spec.name,
                aggregation=ExponentialBucketHistogramAggregation(
                    max_size=HISTOGRAM_MAX_SIZE, max_scale=HISTOGRAM_MAX_SCALE
                ),
            )
        )
    return views


class _StagedResources:
    """Tracks staged OTel resources for rollback before publication."""

    def __init__(self) -> None:
        self.resources: list[Any] = []

    def add(self, resource: Any) -> None:
        self.resources.append(resource)

    def adopt(self, resource: Any) -> None:
        for index, current in enumerate(self.resources):
            if current is resource:
                self.resources.pop(index)
                return

    def rollback(self) -> None:
        timeout_ms = int(EXPORT_TIMEOUT_S * 1000)
        for resource in reversed(self.resources):
            _shutdown_provider(resource, timeout_ms)
        self.resources.clear()

    def transfer(self, handle: RuntimeHandle, tracer: Any, meter: Any, logger_prov: Any) -> None:
        handle._tracer_provider = tracer
        handle._meter_provider = meter
        handle._logger_provider = logger_prov
        self.resources.clear()


def _attach_otel_logging(
    handle: RuntimeHandle,
    handler: logging.Handler,
    role: str,
    log_level: int,
    file_entry: _HandlerEntry | None,
) -> None:
    handler.addFilter(_ExcludeOtelRecursion())
    handle.add_handler(handler, "theater", is_otel=True)
    handle.set_logger_propagate("theater", False)
    handle.set_logger_level("theater", log_level)
    if file_entry is not None:
        handle.share_handler(file_entry, "opentelemetry")
    elif role != PROCESS_ROLE_DAEMON:
        handle.add_handler(logging.NullHandler(), "opentelemetry")
    handle.set_logger_level("opentelemetry", logging.NOTSET)
    handle.set_logger_propagate("opentelemetry", False)


def _stage_otel(
    handle: RuntimeHandle,
    role: str,
    protocol: str,
    endpoint: str | None,
    service_name: str,
    export_interval_ms: int,
    log_level: int,
    file_entry: _HandlerEntry | None,
    metric_specs: tuple[MetricSpec, ...],
) -> None:
    """Build, publish, and attach OTel providers with staged rollback."""
    from opentelemetry.sdk.resources import Resource

    _check_existing_provider()
    staged = _StagedResources()

    version = _get_version()
    resource = Resource.create(
        {"service.name": service_name, "service.version": version, "theater.process.role": role}
    )

    otel_handler: logging.Handler | None = None
    try:
        endpoints = _resolve_endpoints(protocol, endpoint)
        trace_exp, metric_exp, log_exp = _build_exporters(protocol, endpoints, staged)
        _check_existing_provider()

        views = _build_views(metric_specs)
        tracer_provider, meter_provider, logger_provider = _stage_providers(
            staged, resource, trace_exp, metric_exp, log_exp, export_interval_ms, views
        )

        # Build registry, bridge, gauge cache.
        from theater.observability.catalog import OPERATIONS
        from theater.observability.engine import set_metric_bridge
        from theater.observability.metrics import (
            CounterRegistry,
            GaugeCache,
            HistogramRegistry,
            MetricBridge,
        )

        meter = meter_provider.get_meter("theater", version)
        registry = HistogramRegistry(meter=meter)
        counter_registry = CounterRegistry(meter=meter)
        bridge = MetricBridge(registry, counter_registry)
        for spec in OPERATIONS:
            if spec.metric_name is not None:
                bridge.register_histogram(spec.metric_name, spec.description or "", spec.unit)
        bridge.register_specs(metric_specs)

        gauge_cache = GaugeCache()
        gauge_cache.register_observable_gauges(meter)
        bridge.set_gauge_cache(gauge_cache)

        from opentelemetry.sdk._logs import LoggingHandler

        otel_handler = LoggingHandler(logger_provider=logger_provider)

        from opentelemetry.trace import get_tracer_provider, set_tracer_provider

        set_tracer_provider(tracer_provider)
        if get_tracer_provider() is not tracer_provider:
            staged.rollback()
            raise ObservabilityError("tracer provider race lost")  # noqa: TRY301

        _attach_otel_logging(handle, otel_handler, role, log_level, file_entry)

        handle._metric_bridge = bridge
        set_metric_bridge(bridge)
        staged.transfer(handle, tracer_provider, meter_provider, logger_provider)

    except Exception:
        staged.rollback()
        if otel_handler is not None and all(
            entry.handler is not otel_handler for entry in handle._handler_entries
        ):
            with contextlib.suppress(Exception):
                otel_handler.close()
        raise


def _stage_providers(
    staged: _StagedResources,
    resource: Any,
    trace_exp: Any,
    metric_exp: Any,
    log_exp: Any,
    export_interval_ms: int,
    views: list[Any],
) -> tuple[Any, Any, Any]:
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    timeout_ms = int(EXPORT_TIMEOUT_S * 1000)

    tracer_provider = TracerProvider(resource=resource, shutdown_on_exit=False)
    staged.add(tracer_provider)
    span_processor = BatchSpanProcessor(
        trace_exp,
        max_queue_size=BATCH_QUEUE_SIZE,
        max_export_batch_size=EXPORT_BATCH_SIZE,
        schedule_delay_millis=export_interval_ms,
        export_timeout_millis=timeout_ms,
    )
    staged.add(span_processor)
    staged.adopt(trace_exp)
    tracer_provider.add_span_processor(span_processor)
    staged.adopt(span_processor)

    metric_reader = PeriodicExportingMetricReader(
        metric_exp, export_interval_millis=export_interval_ms, export_timeout_millis=timeout_ms
    )
    staged.add(metric_reader)
    staged.adopt(metric_exp)
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[metric_reader],
        shutdown_on_exit=False,
        views=views,
    )
    staged.add(meter_provider)
    staged.adopt(metric_reader)

    logger_provider = LoggerProvider(resource=resource, shutdown_on_exit=False)
    staged.add(logger_provider)
    log_processor = BatchLogRecordProcessor(
        log_exp,
        max_queue_size=BATCH_QUEUE_SIZE,
        max_export_batch_size=EXPORT_BATCH_SIZE,
        schedule_delay_millis=export_interval_ms,
        export_timeout_millis=timeout_ms,
    )
    staged.add(log_processor)
    staged.adopt(log_exp)
    logger_provider.add_log_record_processor(log_processor)
    staged.adopt(log_processor)

    return tracer_provider, meter_provider, logger_provider


def is_configured() -> bool:
    return _configured
