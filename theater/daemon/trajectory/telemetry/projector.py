"""Projection of accepted agent batches into bounded metric observations."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from theater.constants.observability import (
    AGENT_COST_METRIC,
    AGENT_FAILURES_METRIC,
    AGENT_REQUEST_DURATION_METRIC,
    AGENT_REQUEST_TTFT_METRIC,
    AGENT_RESULT_CANCELLED,
    AGENT_RESULT_ERROR,
    AGENT_RESULT_INTERRUPTED,
    AGENT_RESULT_SUCCESS,
    AGENT_RESULT_TIMEOUT,
    AGENT_TELEMETRY_UNKNOWN_LABEL,
    AGENT_TOKEN_KIND_CACHE_READ,
    AGENT_TOKEN_KIND_CACHE_WRITE,
    AGENT_TOKEN_KIND_INPUT,
    AGENT_TOKEN_KIND_OUTPUT,
    AGENT_TOKEN_KIND_REASONING,
    AGENT_TOKENS_METRIC,
    AGENT_TOOL_DURATION_METRIC,
)
from theater.daemon.trajectory.history import source_epoch_for
from theater.daemon.trajectory.project import project_batch
from theater.harness.contracts.events import Event, TokenUsage
from theater.harness.contracts.source import Batch
from theater.observability.metrics import MetricBridge, MetricSpec
from theater.pricing import estimate_cost_usd
from theater.trajectory import (
    CostProvenance,
    Timing,
    TimingProvenance,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryRequest,
    TrajectoryStatus,
    TrajectoryToolOperation,
    requests_for_records,
    tool_operations_for_records,
)

from .catalog import AGENT_METRIC_SPECS, metric_spec
from .labels import AgentMetricLabels, normalize_label
from .state import AgentTelemetryState, ParticipantEmissionState

_TERMINAL_STATUSES = frozenset(
    {
        TrajectoryStatus.COMPLETED,
        TrajectoryStatus.ERROR,
        TrajectoryStatus.TIMEOUT,
        TrajectoryStatus.CANCELLED,
        TrajectoryStatus.INTERRUPTED,
    }
)
_FAILURE_STATUSES = frozenset(
    {
        TrajectoryStatus.ERROR,
        TrajectoryStatus.TIMEOUT,
        TrajectoryStatus.CANCELLED,
        TrajectoryStatus.INTERRUPTED,
    }
)
_RESULTS = {
    TrajectoryStatus.COMPLETED: AGENT_RESULT_SUCCESS,
    TrajectoryStatus.ERROR: AGENT_RESULT_ERROR,
    TrajectoryStatus.TIMEOUT: AGENT_RESULT_TIMEOUT,
    TrajectoryStatus.CANCELLED: AGENT_RESULT_CANCELLED,
    TrajectoryStatus.INTERRUPTED: AGENT_RESULT_INTERRUPTED,
}
_COST_PROVENANCE = frozenset(provenance.value for provenance in CostProvenance)
_REQUEST_DURATION_SPEC = metric_spec(AGENT_REQUEST_DURATION_METRIC)
_REQUEST_TTFT_SPEC = metric_spec(AGENT_REQUEST_TTFT_METRIC)
_TOKENS_SPEC = metric_spec(AGENT_TOKENS_METRIC)
_COST_SPEC = metric_spec(AGENT_COST_METRIC)
_TOOL_DURATION_SPEC = metric_spec(AGENT_TOOL_DURATION_METRIC)
_FAILURES_SPEC = metric_spec(AGENT_FAILURES_METRIC)


class AgentTelemetry:
    """Projects bounded process telemetry without retaining trajectory records."""

    def __init__(self, store: Any, bridge: MetricBridge) -> None:
        self._store = store
        self._bridge = bridge
        self._labels = AgentMetricLabels()
        self._state = AgentTelemetryState()

    def record_batch(
        self,
        participant_id: str,
        batch: Batch,
        new_usage_events: tuple[Event, ...],
    ) -> None:
        """Emit accepted usage and direct terminal signals from one source batch."""
        participant = self._store.get_participant(participant_id)
        if participant is None or not getattr(self._bridge, "active", True):
            return
        location = (
            batch.attached.location
            if batch.attached is not None
            else participant.transcript_location
        )
        source_epoch = source_epoch_for(participant, location)
        state = self._state.for_participant(participant_id, source_epoch)
        harness = normalize_label(participant.harness)
        self._record_usage(harness, new_usage_events)
        records = project_batch(batch, participant_id=participant_id, source_epoch=source_epoch)
        self._record_requests(state, harness, records)
        self._record_tools(state, harness, records)
        for record in records:
            self._record_failure(state, harness, record)

    def discard(self, participant_id: str) -> None:
        """Forget deduplication state for a discarded participant stream."""
        self._state.discard(participant_id)

    def _record_usage(self, harness: str, events: tuple[Event, ...]) -> None:
        for event in events:
            usage = event.usage
            if usage is None:
                continue
            model = self._labels.model(usage.model)
            counts = (
                (AGENT_TOKEN_KIND_INPUT, _positive_tokens(usage.input_tokens)),
                (AGENT_TOKEN_KIND_OUTPUT, _positive_tokens(usage.output_tokens)),
                (AGENT_TOKEN_KIND_REASONING, _positive_tokens(usage.reasoning_output_tokens)),
                (AGENT_TOKEN_KIND_CACHE_READ, _positive_tokens(usage.cache_read_input_tokens)),
                (AGENT_TOKEN_KIND_CACHE_WRITE, _positive_tokens(usage.cache_creation_input_tokens)),
            )
            for kind, count in counts:
                if count:
                    self._bridge.observe(
                        _TOKENS_SPEC,
                        count,
                        {"harness": harness, "model": model, "kind": kind},
                    )
            cost, provenance = _usage_cost(usage)
            if cost is not None:
                self._bridge.observe(
                    _COST_SPEC,
                    cost,
                    {"harness": harness, "model": model, "provenance": provenance},
                )

    def _record_requests(
        self,
        state: ParticipantEmissionState,
        harness: str,
        records: tuple[TrajectoryRecord, ...],
    ) -> None:
        request_records = tuple(
            record
            for record in records
            if record.kind not in {TrajectoryKind.CONTEXT, TrajectoryKind.SYSTEM}
        )
        requests = requests_for_records(request_records)
        associated = {record_id for request in requests for record_id in request.model_record_ids}
        for request in requests:
            if request.model_record_ids:
                self._record_request(state, harness, request)
        for record in records:
            if record.record_id not in associated:
                self._record_fallback_request(state, harness, record)

    def _record_request(
        self,
        state: ParticipantEmissionState,
        harness: str,
        request: TrajectoryRequest,
    ) -> None:
        self._record_request_timing(
            state,
            harness,
            request.request_id,
            request.status,
            request.timing,
            request.model,
        )

    def _record_fallback_request(
        self,
        state: ParticipantEmissionState,
        harness: str,
        record: TrajectoryRecord,
    ) -> None:
        if record.lane is not TrajectoryLane.MODEL or record.kind in {
            TrajectoryKind.CONTEXT,
            TrajectoryKind.SYSTEM,
        }:
            return
        self._record_request_timing(
            state,
            harness,
            record.record_id,
            record.status,
            record.timing,
            record.usage.model if record.usage is not None else None,
        )

    def _record_request_timing(
        self,
        state: ParticipantEmissionState,
        harness: str,
        request_id: str,
        status: TrajectoryStatus,
        timing: Timing | None,
        model: str | None,
    ) -> None:
        if status not in _TERMINAL_STATUSES or not _has_direct_timing(timing):
            return
        assert timing is not None
        attributes = {
            "harness": harness,
            "model": self._labels.model(model),
            "result": _RESULTS[status],
            "timing_provenance": timing.provenance.value,
        }
        if timing.duration_ms is not None:
            self._observe_signal(
                state,
                _REQUEST_DURATION_SPEC,
                request_id,
                timing.duration_ms,
                attributes,
            )
        ttft_ms = timing.ttft_ms
        if ttft_ms is not None:
            self._observe_signal(
                state,
                _REQUEST_TTFT_SPEC,
                request_id,
                ttft_ms,
                attributes,
            )

    def _record_tools(
        self,
        state: ParticipantEmissionState,
        harness: str,
        records: tuple[TrajectoryRecord, ...],
    ) -> None:
        by_record_id = {record.record_id: record for record in records}
        for operation in tool_operations_for_records(records):
            members = tuple(
                by_record_id[record_id]
                for record_id in (*operation.call_record_ids, *operation.result_record_ids)
                if record_id in by_record_id
            )
            self._record_tool(state, harness, operation, members)

    def _record_tool(
        self,
        state: ParticipantEmissionState,
        harness: str,
        operation: TrajectoryToolOperation,
        members: Sequence[TrajectoryRecord],
    ) -> None:
        if operation.status not in _TERMINAL_STATUSES or not _has_direct_timing(operation.timing):
            return
        assert operation.timing is not None
        if operation.timing.duration_ms is None:
            return
        self._observe_signal(
            state,
            _TOOL_DURATION_SPEC,
            operation.operation_id,
            operation.timing.duration_ms,
            {
                "harness": harness,
                "tool": self._tool_label(members),
                "result": _RESULTS[operation.status],
                "timing_provenance": operation.timing.provenance.value,
            },
        )

    def _record_failure(
        self,
        state: ParticipantEmissionState,
        harness: str,
        record: TrajectoryRecord,
    ) -> None:
        if record.status not in _TERMINAL_STATUSES:
            return
        if record.failure is None and record.status not in _FAILURE_STATUSES:
            return
        self._observe_signal(
            state,
            _FAILURES_SPEC,
            record.record_id,
            1,
            {"harness": harness, "category": normalize_label(_failure_category(record))},
        )

    def _tool_label(self, records: Sequence[TrajectoryRecord]) -> str:
        for record in reversed(records):
            mcp_tool = normalize_label(getattr(record, "mcp_tool", None))
            if mcp_tool == AGENT_TELEMETRY_UNKNOWN_LABEL:
                continue
            mcp_server = normalize_label(getattr(record, "mcp_server", None))
            value = (
                f"{mcp_server}/{mcp_tool}"
                if mcp_server != AGENT_TELEMETRY_UNKNOWN_LABEL
                else mcp_tool
            )
            return self._labels.tool(value)
        for record in reversed(records):
            for detail in reversed(record.details):
                if detail.name != "tool":
                    continue
                label = normalize_label(detail.preview.text)
                if label != AGENT_TELEMETRY_UNKNOWN_LABEL:
                    return self._labels.tool(label)
        return AGENT_TELEMETRY_UNKNOWN_LABEL

    def _observe_signal(
        self,
        state: ParticipantEmissionState,
        spec: MetricSpec,
        signal_id: str,
        value: float | int,
        attributes: dict[str, str],
    ) -> None:
        if self._state.contains(state, spec.name, signal_id):
            return
        self._bridge.observe(spec, value, attributes)
        self._state.remember(state, spec.name, signal_id)


def create_agent_telemetry(
    store: Any,
    bridge: MetricBridge | None,
    *,
    enabled: bool,
) -> AgentTelemetry | None:
    """Build active agent telemetry after registering its feature-specific metrics."""
    if not enabled or bridge is None or not bridge.active:
        return None
    bridge.register_specs(AGENT_METRIC_SPECS)
    return AgentTelemetry(store, bridge)


def _has_direct_timing(timing: Timing | None) -> bool:
    return (
        timing is not None
        and timing.provenance is not TimingProvenance.UNAVAILABLE
        and (timing.duration_ms is not None or timing.ttft_ms is not None)
    )


def _failure_category(record: TrajectoryRecord) -> str:
    if record.failure is not None:
        return record.failure.category.value
    if record.status is TrajectoryStatus.TIMEOUT:
        return TrajectoryStatus.TIMEOUT.value
    return AGENT_TELEMETRY_UNKNOWN_LABEL


def _usage_cost(usage: TokenUsage) -> tuple[float | None, str]:
    reported = _nonnegative_number(usage.cost_usd)
    if reported is not None:
        provenance = normalize_label(getattr(usage.cost_provenance, "value", usage.cost_provenance))
        return (
            reported,
            provenance if provenance in _COST_PROVENANCE else AGENT_TELEMETRY_UNKNOWN_LABEL,
        )
    estimated = estimate_cost_usd(
        usage.model,
        provider=usage.provider,
        input_tokens=_positive_tokens(usage.input_tokens),
        output_tokens=_positive_tokens(usage.output_tokens),
        cache_read_tokens=_positive_tokens(usage.cache_read_input_tokens),
        cache_write_tokens=_positive_tokens(usage.cache_creation_input_tokens),
        reasoning_tokens=_positive_tokens(usage.reasoning_output_tokens),
    )
    return _nonnegative_number(estimated), CostProvenance.ESTIMATED.value


def _positive_tokens(value: object) -> int:
    return value if type(value) is int and value > 0 else 0


def _nonnegative_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


__all__ = ["AgentTelemetry", "create_agent_telemetry"]
