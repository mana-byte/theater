"""Observability: process-level logging, tracing, metrics, and timing."""

from __future__ import annotations

from theater.observability.catalog import (
    OPERATIONS,
    RESULTS,
    AttrMapping,
    OperationSpec,
    TraceKind,
    ValueTransform,
)
from theater.observability.engine import (
    emit,
    enable_trace,
    lag_monitor,
    metric_bridge,
    metric_bridge_active,
    ready_lag,
    set_metric_bridge,
    span,
)
from theater.observability.metrics import (
    CounterRegistry,
    GaugeCache,
    GaugeSampler,
    HistogramRegistry,
    MetricBridge,
    MetricKind,
    MetricSpec,
    create_active_gauge_sampler,
)
from theater.observability.runtime import (
    ObservabilityError,
    RuntimeHandle,
    configure,
    is_configured,
)
from theater.observability.tracing import (
    extract_trace_context,
    inject_trace_context,
    start_span,
)

__all__ = [
    "OPERATIONS",
    "RESULTS",
    "AttrMapping",
    "CounterRegistry",
    "GaugeCache",
    "GaugeSampler",
    "HistogramRegistry",
    "MetricBridge",
    "MetricKind",
    "MetricSpec",
    "ObservabilityError",
    "OperationSpec",
    "RuntimeHandle",
    "TraceKind",
    "ValueTransform",
    "configure",
    "create_active_gauge_sampler",
    "emit",
    "enable_trace",
    "extract_trace_context",
    "inject_trace_context",
    "is_configured",
    "lag_monitor",
    "metric_bridge",
    "metric_bridge_active",
    "ready_lag",
    "set_metric_bridge",
    "span",
    "start_span",
]
