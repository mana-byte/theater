"""Metric emission for projected agent trajectory operations."""

from __future__ import annotations

import math
from collections.abc import Sequence

from theater.constants.observability import (
    AGENT_COST_METRIC,
    AGENT_FAILURES_METRIC,
    AGENT_REQUEST_DURATION_METRIC,
    AGENT_REQUEST_TTFT_METRIC,
    AGENT_REQUESTS_METRIC,
    AGENT_TELEMETRY_UNKNOWN_LABEL,
    AGENT_TOKEN_KIND_CACHE_READ,
    AGENT_TOKEN_KIND_CACHE_WRITE,
    AGENT_TOKEN_KIND_INPUT,
    AGENT_TOKEN_KIND_OUTPUT,
    AGENT_TOKEN_KIND_REASONING,
    AGENT_TOKENS_METRIC,
    AGENT_TOOL_CALLS_METRIC,
    AGENT_TOOL_DURATION_METRIC,
)
from theater.harness.contracts.events import Event, TokenUsage
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
)

from .catalog import metric_spec
from .labels import AgentMetricLabels, metric_tool_label, normalize_label
from .semantics import FAILURE_STATUSES, RESULTS, TERMINAL_STATUSES, final_tool_operation
from .state import AgentTelemetryState, ParticipantEmissionState

_COST_PROVENANCE = frozenset(provenance.value for provenance in CostProvenance)
_REQUEST_DURATION_SPEC = metric_spec(AGENT_REQUEST_DURATION_METRIC)
_REQUEST_TTFT_SPEC = metric_spec(AGENT_REQUEST_TTFT_METRIC)
_TOKENS_SPEC = metric_spec(AGENT_TOKENS_METRIC)
_COST_SPEC = metric_spec(AGENT_COST_METRIC)
_TOOL_DURATION_SPEC = metric_spec(AGENT_TOOL_DURATION_METRIC)
_FAILURES_SPEC = metric_spec(AGENT_FAILURES_METRIC)
_REQUESTS_SPEC = metric_spec(AGENT_REQUESTS_METRIC)
_TOOL_CALLS_SPEC = metric_spec(AGENT_TOOL_CALLS_METRIC)


class AgentMetricEmitter:
    """Emit bounded metric observations without retaining trajectory values."""

    def __init__(
        self,
        bridge: MetricBridge,
        labels: AgentMetricLabels,
        state: AgentTelemetryState,
    ) -> None:
        self._bridge = bridge
        self._labels = labels
        self._state = state

    def record(
        self,
        state: ParticipantEmissionState,
        harness: str,
        usage_events: tuple[Event, ...],
        records: tuple[TrajectoryRecord, ...],
        requests: tuple[TrajectoryRequest, ...],
        tools: tuple[TrajectoryToolOperation, ...],
        operation_records: tuple[TrajectoryRecord, ...],
    ) -> None:
        """Project accepted usage, requests, tools, and failures into metrics."""
        self._record_usage(harness, usage_events)
        self._record_requests(state, harness, records, requests)
        self._record_tools(state, harness, operation_records, tools)
        for record in records:
            self._record_failure(state, harness, record)

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
                    self._observe_unkeyed(
                        _TOKENS_SPEC, count, {"harness": harness, "model": model, "kind": kind}
                    )
            cost, provenance = _usage_cost(usage)
            if cost is not None:
                self._observe_unkeyed(
                    _COST_SPEC,
                    cost,
                    {"harness": harness, "model": model, "provenance": provenance},
                )

    def _record_requests(
        self,
        state: ParticipantEmissionState,
        harness: str,
        records: tuple[TrajectoryRecord, ...],
        requests: tuple[TrajectoryRequest, ...],
    ) -> None:
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
        self._record_request_operation(
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
        self._record_request_operation(
            state,
            harness,
            record.record_id,
            record.status,
            record.timing,
            record.usage.model if record.usage is not None else None,
        )

    def _record_request_operation(
        self,
        state: ParticipantEmissionState,
        harness: str,
        request_id: str,
        status: TrajectoryStatus,
        timing: Timing | None,
        model_value: str | None,
    ) -> None:
        if status not in TERMINAL_STATUSES:
            return
        model = self._labels.model(model_value)
        attributes = {"harness": harness, "model": model, "result": RESULTS[status]}
        self._observe_signal(state, _REQUESTS_SPEC, request_id, 1, attributes)
        if not _has_direct_timing(timing):
            return
        assert timing is not None
        timed_attributes = {**attributes, "timing_provenance": timing.provenance.value}
        if timing.duration_ms is not None:
            self._observe_signal(
                state,
                _REQUEST_DURATION_SPEC,
                request_id,
                timing.duration_ms,
                timed_attributes,
            )
        if timing.ttft_ms is not None:
            self._observe_signal(
                state,
                _REQUEST_TTFT_SPEC,
                request_id,
                timing.ttft_ms,
                timed_attributes,
            )

    def _record_tools(
        self,
        state: ParticipantEmissionState,
        harness: str,
        records: tuple[TrajectoryRecord, ...],
        tools: tuple[TrajectoryToolOperation, ...],
    ) -> None:
        by_record_id = {record.record_id: record for record in records}
        for operation in tools:
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
        if not final_tool_operation(operation):
            return
        tool = metric_tool_label(members, self._labels)
        attributes = {"harness": harness, "tool": tool, "result": RESULTS[operation.status]}
        self._observe_signal(state, _TOOL_CALLS_SPEC, operation.operation_id, 1, attributes)
        if not _has_direct_timing(operation.timing) or operation.timing is None:
            return
        if operation.timing.duration_ms is None:
            return
        self._observe_signal(
            state,
            _TOOL_DURATION_SPEC,
            operation.operation_id,
            operation.timing.duration_ms,
            {**attributes, "timing_provenance": operation.timing.provenance.value},
        )

    def _record_failure(
        self,
        state: ParticipantEmissionState,
        harness: str,
        record: TrajectoryRecord,
    ) -> None:
        if record.status not in TERMINAL_STATUSES:
            return
        if record.failure is None and record.status not in FAILURE_STATUSES:
            return
        self._observe_signal(
            state,
            _FAILURES_SPEC,
            record.record_id,
            1,
            {"harness": harness, "category": normalize_label(_failure_category(record))},
        )

    def _observe_unkeyed(
        self,
        spec: MetricSpec,
        value: float | int,
        attributes: dict[str, str],
    ) -> None:
        try:
            self._bridge.observe(spec, value, attributes)
        except Exception:
            return

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
        try:
            self._bridge.observe(spec, value, attributes)
        except Exception:
            return
        self._state.remember(state, spec.name, signal_id)


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


__all__ = ["FAILURE_STATUSES", "RESULTS", "TERMINAL_STATUSES", "AgentMetricEmitter"]
