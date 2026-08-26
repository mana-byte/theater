"""Completed request and tool span emission for agent trajectory telemetry."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from theater.constants.observability import AGENT_REQUEST_SPAN, AGENT_TOOL_SPAN
from theater.observability.catalog import TraceKind
from theater.trajectory import (
    Timing,
    TimingProvenance,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryRequest,
    TrajectoryToolOperation,
)

from .attributes import Scalar, optional
from .labels import normalize_label, tool_identity
from .semantics import (
    RESULTS,
    TERMINAL_STATUSES,
    final_tool_operation,
    operation_error,
    operation_error_type,
)
from .state import AgentTelemetryState, ParticipantEmissionState

_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


class AgentSpanEmitter:
    """Emit terminal operation spans and return their member-record contexts."""

    def __init__(self, bridge: Any, state: AgentTelemetryState) -> None:
        self._bridge = bridge
        self._state = state

    def record(
        self,
        state: ParticipantEmissionState,
        participant: Any,
        harness: str,
        records: tuple[TrajectoryRecord, ...],
        requests: tuple[TrajectoryRequest, ...],
        tools: tuple[TrajectoryToolOperation, ...],
        operation_records: tuple[TrajectoryRecord, ...],
    ) -> dict[str, Any]:
        """Emit request roots, linked tools, and compatible nested tool children."""
        contexts: dict[str, Any] = {}
        current_ids = {record.record_id for record in records}
        request_contexts: dict[str, Any] = {}
        associated = {record_id for request in requests for record_id in request.model_record_ids}
        for request in requests:
            if current_ids.intersection(request.model_record_ids):
                context = self._request_span(state, participant, harness, request)
                if context is not None:
                    self._map_context(contexts, request.record_ids, context)
                    if request.source_request_id is not None:
                        request_contexts[request.source_request_id] = context
        for record in records:
            if record.record_id not in associated:
                context = self._fallback_request_span(state, participant, harness, record)
                if context is not None:
                    contexts[record.record_id] = context
        by_record_id = {record.record_id: record for record in operation_records}
        for operation in tools:
            members = tuple(
                by_record_id[record_id]
                for record_id in (*operation.call_record_ids, *operation.result_record_ids)
                if record_id in by_record_id
            )
            context = self._tool_span(
                state,
                participant,
                harness,
                operation,
                members,
                request_contexts.get(operation.request_id)
                if operation.request_id is not None
                else None,
            )
            if context is None:
                continue
            self._map_context(
                contexts,
                (*operation.call_record_ids, *operation.result_record_ids),
                context,
            )
        return contexts

    def _request_span(
        self,
        state: ParticipantEmissionState,
        participant: Any,
        harness: str,
        request: TrajectoryRequest,
    ) -> Any | None:
        interval = _absolute_interval(request.timing)
        cached = self._state.span_context(state, "operation", request.request_id)
        if cached is not None:
            return cached.context
        if request.status not in TERMINAL_STATUSES or interval is None:
            return None
        if self._state.contains(state, AGENT_REQUEST_SPAN, request.request_id):
            return None
        attributes = _common_attributes(participant, harness, request.source_epoch)
        attributes.update(
            {
                "theater.agent.request.id": request.request_id,
                "theater.agent.result": RESULTS[request.status],
            }
        )
        optional(attributes, "theater.agent.request.source_id", request.source_request_id)
        optional(attributes, "theater.agent.turn.id", request.turn_id)
        optional(attributes, "theater.agent.step.id", request.step_id)
        optional(attributes, "theater.agent.model", normalize_label(request.model))
        optional(attributes, "theater.agent.provider", normalize_label(request.provider))
        _timing_attributes(attributes, request.timing)
        _usage_attributes(attributes, request.usage)
        _failure_attributes(
            attributes, request.failure, request.retry_of_record_id, request.retry_attempt
        )
        context = self._emit(
            state,
            AGENT_REQUEST_SPAN,
            request.request_id,
            attributes,
            interval,
            error=operation_error(request.status, request.failure),
            error_type=operation_error_type(request.status, request.failure),
            parent_context=_root_context(),
            kind=TraceKind.CLIENT,
        )
        if context is not None:
            self._state.remember_span_context(
                state, "operation", request.request_id, context, interval
            )
        if context is not None and request.source_request_id is not None:
            self._state.remember_span_context(
                state, "request", request.source_request_id, context, interval
            )
        return context

    def _fallback_request_span(
        self,
        state: ParticipantEmissionState,
        participant: Any,
        harness: str,
        record: TrajectoryRecord,
    ) -> Any | None:
        if record.lane is not TrajectoryLane.MODEL or record.kind in {
            TrajectoryKind.CONTEXT,
            TrajectoryKind.SYSTEM,
        }:
            return None
        interval = _absolute_interval(record.timing)
        cached = self._state.span_context(state, "operation", record.record_id)
        if cached is not None:
            return cached.context
        if record.status not in TERMINAL_STATUSES or interval is None:
            return None
        if self._state.contains(state, AGENT_REQUEST_SPAN, record.record_id):
            return None
        attributes = _common_attributes(participant, harness, record.source_epoch)
        attributes.update(
            {
                "theater.agent.request.id": record.record_id,
                "theater.agent.result": RESULTS[record.status],
            }
        )
        optional(attributes, "theater.agent.request.source_id", record.request_id)
        optional(attributes, "theater.agent.turn.id", record.turn_id)
        optional(attributes, "theater.agent.step.id", record.step_id)
        optional(
            attributes,
            "theater.agent.model",
            normalize_label(record.usage.model if record.usage else None),
        )
        optional(
            attributes,
            "theater.agent.provider",
            normalize_label(record.usage.provider if record.usage else None),
        )
        _timing_attributes(attributes, record.timing)
        _usage_attributes(attributes, record.usage)
        _failure_attributes(
            attributes, record.failure, record.retry_of_record_id, record.retry_attempt
        )
        context = self._emit(
            state,
            AGENT_REQUEST_SPAN,
            record.record_id,
            attributes,
            interval,
            error=operation_error(record.status, record.failure),
            error_type=operation_error_type(record.status, record.failure),
            parent_context=_root_context(),
            kind=TraceKind.CLIENT,
        )
        if context is not None:
            self._state.remember_span_context(
                state, "operation", record.record_id, context, interval
            )
        if context is not None and record.request_id is not None:
            self._state.remember_span_context(
                state, "request", record.request_id, context, interval
            )
        return context

    def _tool_span(
        self,
        state: ParticipantEmissionState,
        participant: Any,
        harness: str,
        operation: TrajectoryToolOperation,
        members: Sequence[TrajectoryRecord],
        request_context: Any | None,
    ) -> Any | None:
        interval = _absolute_interval(operation.timing)
        cached = self._state.span_context(state, "operation", operation.operation_id)
        if cached is not None:
            return cached.context
        if not final_tool_operation(operation) or interval is None:
            return None
        if self._state.contains(state, AGENT_TOOL_SPAN, operation.operation_id):
            return None
        attributes = _common_attributes(participant, harness, operation.source_epoch)
        attributes.update(
            {
                "theater.agent.operation.id": operation.operation_id,
                "theater.agent.tool": tool_identity(members),
                "theater.agent.result": RESULTS[operation.status],
            }
        )
        optional(attributes, "theater.agent.request.id", operation.request_id)
        optional(attributes, "theater.agent.call.id", operation.call_id)
        optional(attributes, "theater.agent.parent_call.id", operation.parent_call_id)
        _timing_attributes(attributes, operation.timing)
        _usage_attributes(
            attributes,
            next((record.usage for record in reversed(members) if record.usage is not None), None),
        )
        _failure_attributes(
            attributes,
            operation.failure,
            operation.retry_of_record_id,
            operation.retry_attempt,
        )
        parent_context = (
            self._state.span_context(state, "call", operation.parent_call_id)
            if operation.parent_call_id is not None
            else None
        )
        nested_parent = (
            parent_context.context
            if parent_context is not None and _contains(parent_context, interval)
            else _root_context()
        )
        link_context = _link_context(request_context)
        context = self._emit(
            state,
            AGENT_TOOL_SPAN,
            operation.operation_id,
            attributes,
            interval,
            error=operation_error(operation.status, operation.failure),
            error_type=operation_error_type(operation.status, operation.failure),
            parent_context=nested_parent,
            links=(link_context,) if link_context is not None else (),
            kind=TraceKind.INTERNAL,
        )
        if context is not None:
            self._state.remember_span_context(
                state, "operation", operation.operation_id, context, interval
            )
        if context is not None and operation.call_id is not None:
            self._state.remember_span_context(state, "call", operation.call_id, context, interval)
        return context

    def _emit(
        self,
        state: ParticipantEmissionState,
        span_name: str,
        signal_id: str,
        attributes: dict[str, Scalar],
        interval: tuple[int, int],
        *,
        error: bool,
        error_type: str | None,
        parent_context: Any,
        kind: TraceKind,
        links: Sequence[Any] = (),
    ) -> Any | None:
        try:
            context = self._bridge.emit_span(
                span_name,
                attributes=attributes,
                start_time_ns=interval[0],
                end_time_ns=interval[1],
                parent_context=parent_context,
                links=links,
                kind=kind,
                error=error,
                error_type=error_type,
            )
        except Exception:
            return None
        if context is not None:
            self._state.remember(state, span_name, signal_id)
        return context

    @staticmethod
    def _map_context(contexts: dict[str, Any], record_ids: Sequence[str], context: Any) -> None:
        for record_id in record_ids:
            contexts[record_id] = context


def _common_attributes(
    participant: Any, harness: str, source_epoch: str
) -> dict[str, Scalar]:
    attributes: dict[str, Scalar] = {
        "theater.agent.participant.id": participant.id,
        "theater.agent.harness": harness,
        "theater.agent.source.epoch": source_epoch,
    }
    optional(attributes, "theater.agent.parent.id", getattr(participant, "parent_id", None))
    optional(attributes, "theater.agent.session.id", getattr(participant, "session_id", None))
    return attributes


def _timing_attributes(
    attributes: dict[str, Scalar], timing: Timing | None
) -> None:
    if timing is not None:
        attributes["theater.agent.timing.provenance"] = timing.provenance.value


def _usage_attributes(attributes: dict[str, Scalar], usage: Any | None) -> None:
    if usage is None:
        return
    optional(attributes, "theater.agent.usage.input_tokens", usage.input_tokens)
    optional(attributes, "theater.agent.usage.output_tokens", usage.output_tokens)
    optional(attributes, "theater.agent.usage.reasoning_tokens", usage.reasoning_tokens)
    optional(attributes, "theater.agent.usage.cache_read_tokens", usage.cache_read_tokens)
    optional(attributes, "theater.agent.usage.cache_write_tokens", usage.cache_write_tokens)
    optional(attributes, "theater.agent.cost.usd", usage.cost_usd)
    optional(attributes, "theater.agent.cost.provenance", usage.cost_provenance.value)


def _failure_attributes(
    attributes: dict[str, Scalar],
    failure: Any | None,
    retry_of_record_id: str | None,
    retry_attempt: int | None,
) -> None:
    if failure is not None:
        attributes["theater.agent.failure.category"] = failure.category.value
        optional(attributes, "theater.agent.failure.code", failure.code)
    optional(attributes, "theater.agent.retry.of_record_id", retry_of_record_id)
    optional(attributes, "theater.agent.retry.attempt", retry_attempt)


def _absolute_interval(timing: Timing | None) -> tuple[int, int] | None:
    if timing is None or timing.provenance is TimingProvenance.UNAVAILABLE:
        return None
    start = timing.start
    end = timing.end
    duration_s = timing.duration_ms / 1000 if timing.duration_ms is not None else None
    if start is None and end is not None and duration_s is not None:
        start = end - duration_s
    elif end is None and start is not None and duration_s is not None:
        end = start + duration_s
    if start is None or end is None or not start <= end:
        return None
    start_ns = _epoch_ns(start)
    end_ns = _epoch_ns(end)
    return (start_ns, end_ns) if start_ns is not None and end_ns is not None else None


def _contains(parent: Any, child: tuple[int, int]) -> bool:
    """Return whether a retained parent interval fully contains the child interval."""
    return parent.start_time_ns <= child[0] and child[1] <= parent.end_time_ns


def _link_context(context: Any) -> Any | None:
    """Extract a real SpanContext for a Link without creating trace identity."""
    try:
        from opentelemetry.trace import get_current_span

        span_context = get_current_span(context).get_span_context()
    except Exception:
        return None
    return span_context if span_context.is_valid else None


def _epoch_ns(seconds: float) -> int | None:
    if not math.isfinite(seconds):
        return None
    try:
        value = seconds * 1_000_000_000
    except OverflowError:
        return None
    if not math.isfinite(value) or not _INT64_MIN <= value <= _INT64_MAX:
        return None
    return int(value)


def _root_context() -> Any:
    try:
        from opentelemetry.context import Context

        return Context()
    except Exception:
        return None


__all__ = ["AgentSpanEmitter"]
