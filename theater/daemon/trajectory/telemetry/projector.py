"""Orchestration of independent agent trajectory telemetry signals."""

from __future__ import annotations

import contextlib
from typing import Any

from theater.daemon.trajectory.history import source_epoch_for
from theater.daemon.trajectory.project import project_batch
from theater.harness.contracts.events import Event
from theater.harness.contracts.source import Batch
from theater.observability.metrics import MetricBridge
from theater.trajectory import requests_for_records, tool_operations_for_records

from .catalog import AGENT_METRIC_SPECS
from .labels import AgentMetricLabels, normalize_label
from .logs import AgentLogEmitter
from .metrics import AgentMetricEmitter
from .spans import AgentSpanEmitter
from .state import AgentTelemetryState


class AgentTelemetry:
    """Projects an accepted source batch once and fans it out to active signals."""

    def __init__(
        self,
        store: Any,
        metric_bridge: MetricBridge | None = None,
        signal_bridge: Any | None = None,
        *,
        metrics_enabled: bool = True,
        logs_enabled: bool = False,
        spans_enabled: bool = False,
        include_log_content: bool = False,
    ) -> None:
        self._store = store
        self._metric_bridge = metric_bridge
        self._signal_bridge = signal_bridge
        self._metrics_enabled = metrics_enabled
        self._logs_enabled = logs_enabled
        self._spans_enabled = spans_enabled
        self._labels = AgentMetricLabels()
        self._state = AgentTelemetryState()
        self._metrics = (
            AgentMetricEmitter(metric_bridge, self._labels, self._state)
            if metric_bridge is not None
            else None
        )
        self._logs = (
            AgentLogEmitter(signal_bridge, self._state, include_content=include_log_content)
            if signal_bridge is not None
            else None
        )
        self._spans = (
            AgentSpanEmitter(signal_bridge, self._state)
            if signal_bridge is not None
            else None
        )

    def record_batch(
        self,
        participant_id: str,
        batch: Batch,
        new_usage_events: tuple[Event, ...],
    ) -> None:
        """Project one accepted batch once, then isolate its enabled signal emissions."""
        participant = self._store.get_participant(participant_id)
        if participant is None or not self._active:
            return
        location = (
            batch.attached.location
            if batch.attached is not None
            else participant.transcript_location
        )
        source_epoch = source_epoch_for(participant, location)
        state = self._state.for_participant(participant_id, source_epoch)
        harness = normalize_label(participant.harness)
        records = project_batch(batch, participant_id=participant_id, source_epoch=source_epoch)
        self._state.merge_records(state, records)
        projection_records = self._state.records_for_projection(state)
        current_records = tuple(
            record
            for record in records
            if (retained := state.records.get(record.record_id)) is not None
            and retained.revision == record.revision
        )
        current_ids = {record.record_id for record in current_records}
        requests = tuple(
            request
            for request in requests_for_records(projection_records)
            if current_ids.intersection(request.record_ids)
        )
        tools = tuple(
            operation
            for operation in tool_operations_for_records(projection_records)
            if current_ids.intersection((*operation.call_record_ids, *operation.result_record_ids))
        )
        if self._metrics_active and self._metrics is not None:
            with contextlib.suppress(Exception):
                self._metrics.record(
                    state,
                    harness,
                    new_usage_events,
                    current_records,
                    requests,
                    tools,
                    projection_records,
                )
        contexts: dict[str, Any] = {}
        if self._spans_active and self._spans is not None:
            try:
                contexts = self._spans.record(
                    state,
                    participant,
                    harness,
                    current_records,
                    requests,
                    tools,
                    projection_records,
                )
            except Exception:
                contexts = {}
        if self._logs_active and self._logs is not None:
            with contextlib.suppress(Exception):
                self._logs.record(state, participant, harness, current_records, contexts)

    def discard(self, participant_id: str) -> None:
        """Forget deduplication state for a discarded participant stream."""
        self._state.discard(participant_id)

    @property
    def _active(self) -> bool:
        return self._metrics_active or self._logs_active or self._spans_active

    @property
    def _metrics_active(self) -> bool:
        return (
            self._metrics_enabled
            and self._metric_bridge is not None
            and getattr(self._metric_bridge, "active", True)
        )

    @property
    def _logs_active(self) -> bool:
        return (
            self._logs_enabled
            and self._signal_bridge is not None
            and getattr(self._signal_bridge, "active", True)
        )

    @property
    def _spans_active(self) -> bool:
        return (
            self._spans_enabled
            and self._signal_bridge is not None
            and getattr(self._signal_bridge, "active", True)
        )


def create_agent_telemetry(
    store: Any,
    metric_bridge: MetricBridge | None,
    signal_bridge: Any | None = None,
    *,
    metrics_enabled: bool | None = None,
    logs_enabled: bool = False,
    spans_enabled: bool = False,
    include_log_content: bool = False,
    enabled: bool | None = None,
) -> AgentTelemetry | None:
    """Create telemetry when at least one requested bridge is active."""
    if metrics_enabled is None:
        metrics_enabled = enabled if enabled is not None else True
    metrics_active = (
        metrics_enabled and metric_bridge is not None and getattr(metric_bridge, "active", True)
    )
    signal_active = (
        (logs_enabled or spans_enabled)
        and signal_bridge is not None
        and getattr(signal_bridge, "active", True)
    )
    if not metrics_active and not signal_active:
        return None
    if metrics_active:
        assert metric_bridge is not None
        metric_bridge.register_specs(AGENT_METRIC_SPECS)
    return AgentTelemetry(
        store,
        metric_bridge,
        signal_bridge,
        metrics_enabled=metrics_enabled,
        logs_enabled=logs_enabled,
        spans_enabled=spans_enabled,
        include_log_content=include_log_content,
    )


__all__ = ["AgentTelemetry", "create_agent_telemetry"]
