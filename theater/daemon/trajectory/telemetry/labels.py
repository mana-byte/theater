"""Bounded label normalization and cardinality policy for agent telemetry."""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence

from theater.constants.observability import (
    AGENT_TELEMETRY_LABEL_MAX_BYTES,
    AGENT_TELEMETRY_MODEL_CARDINALITY_LIMIT,
    AGENT_TELEMETRY_OTHER_LABEL,
    AGENT_TELEMETRY_TOOL_CARDINALITY_LIMIT,
    AGENT_TELEMETRY_UNKNOWN_LABEL,
)
from theater.trajectory import TrajectoryRecord


class AgentMetricLabels:
    """Tracks separate per-process model and tool label admission sets."""

    def __init__(self) -> None:
        self._models: set[str] = set()
        self._tools: set[str] = set()

    def model(self, value: object) -> str:
        """Normalize and bound a model label."""
        return _bounded_cardinality_label(
            value,
            self._models,
            AGENT_TELEMETRY_MODEL_CARDINALITY_LIMIT,
        )

    def tool(self, value: object) -> str:
        """Normalize and bound a tool label."""
        return _bounded_cardinality_label(
            value,
            self._tools,
            AGENT_TELEMETRY_TOOL_CARDINALITY_LIMIT,
        )


def tool_identity(records: Sequence[TrajectoryRecord]) -> str:
    """Return the normalized bounded tool identity without cardinality admission."""
    for record in reversed(records):
        mcp_tool = normalize_label(record.mcp_tool)
        if mcp_tool == AGENT_TELEMETRY_UNKNOWN_LABEL:
            continue
        mcp_server = normalize_label(record.mcp_server)
        value = (
            f"{mcp_server}/{mcp_tool}" if mcp_server != AGENT_TELEMETRY_UNKNOWN_LABEL else mcp_tool
        )
        return normalize_label(value)
    for record in reversed(records):
        for detail in reversed(record.details):
            if detail.name != "tool":
                continue
            label = normalize_label(detail.preview.text)
            if label != AGENT_TELEMETRY_UNKNOWN_LABEL:
                return label
    return AGENT_TELEMETRY_UNKNOWN_LABEL


def metric_tool_label(records: Sequence[TrajectoryRecord], labels: AgentMetricLabels) -> str:
    """Return the metrics-only cardinality-controlled tool identity."""
    return labels.tool(tool_identity(records))


def normalize_label(value: object) -> str:
    """Return a stable bounded metric label or the unknown reserved label."""
    if not isinstance(value, str):
        return AGENT_TELEMETRY_UNKNOWN_LABEL
    value = " ".join(unicodedata.normalize("NFKC", value).split())
    if not value:
        return AGENT_TELEMETRY_UNKNOWN_LABEL
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return AGENT_TELEMETRY_UNKNOWN_LABEL
    if len(encoded) <= AGENT_TELEMETRY_LABEL_MAX_BYTES:
        return value
    truncated = encoded[:AGENT_TELEMETRY_LABEL_MAX_BYTES].decode("utf-8", "ignore")
    return truncated or AGENT_TELEMETRY_UNKNOWN_LABEL


def _bounded_cardinality_label(value: object, seen: set[str], limit: int) -> str:
    label = normalize_label(value)
    if label in {AGENT_TELEMETRY_UNKNOWN_LABEL, AGENT_TELEMETRY_OTHER_LABEL}:
        return label
    if label in seen:
        return label
    if len(seen) >= limit:
        return AGENT_TELEMETRY_OTHER_LABEL
    seen.add(label)
    return label


__all__ = ["AgentMetricLabels", "metric_tool_label", "normalize_label", "tool_identity"]
