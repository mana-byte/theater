"""Feature-specific agent telemetry metric catalog."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from theater.constants.observability import (
    AGENT_COST_METRIC,
    AGENT_FAILURES_METRIC,
    AGENT_REQUEST_DURATION_METRIC,
    AGENT_REQUEST_TTFT_METRIC,
    AGENT_TOKENS_METRIC,
    AGENT_TOOL_DURATION_METRIC,
)
from theater.observability.metrics import MetricKind, MetricSpec

AGENT_METRIC_SPECS: tuple[MetricSpec, ...] = (
    MetricSpec(
        AGENT_REQUEST_DURATION_METRIC,
        "Terminal agent model request duration.",
        "ms",
        MetricKind.HISTOGRAM,
        ("harness", "model", "result", "timing_provenance"),
    ),
    MetricSpec(
        AGENT_REQUEST_TTFT_METRIC,
        "Terminal agent model request time to first token.",
        "ms",
        MetricKind.HISTOGRAM,
        ("harness", "model", "result", "timing_provenance"),
    ),
    MetricSpec(
        AGENT_TOKENS_METRIC,
        "Agent tokens accepted into durable usage accounting.",
        "{token}",
        MetricKind.COUNTER,
        ("harness", "model", "kind"),
    ),
    MetricSpec(
        AGENT_COST_METRIC,
        "Agent cost accepted into durable usage accounting.",
        "USD",
        MetricKind.COUNTER,
        ("harness", "model", "provenance"),
    ),
    MetricSpec(
        AGENT_TOOL_DURATION_METRIC,
        "Terminal agent tool call duration.",
        "ms",
        MetricKind.HISTOGRAM,
        ("harness", "tool", "result", "timing_provenance"),
    ),
    MetricSpec(
        AGENT_FAILURES_METRIC,
        "Terminal agent trajectory failures.",
        "{failure}",
        MetricKind.COUNTER,
        ("harness", "category"),
    ),
)

_SPECS_BY_NAME: Mapping[str, MetricSpec] = MappingProxyType(
    {spec.name: spec for spec in AGENT_METRIC_SPECS}
)


def metric_spec(name: str) -> MetricSpec:
    """Return the registered immutable agent metric specification by name."""
    return _SPECS_BY_NAME[name]


__all__ = ["AGENT_METRIC_SPECS", "metric_spec"]
