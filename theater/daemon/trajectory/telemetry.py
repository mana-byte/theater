"""Bounded agent telemetry projection from live trajectory batches."""

from __future__ import annotations

import math
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
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
    AGENT_TELEMETRY_EMITTED_SIGNAL_LIMIT,
    AGENT_TELEMETRY_LABEL_MAX_BYTES,
    AGENT_TELEMETRY_MODEL_CARDINALITY_LIMIT,
    AGENT_TELEMETRY_OTHER_LABEL,
    AGENT_TELEMETRY_PARTICIPANT_STATE_LIMIT,
    AGENT_TELEMETRY_TOOL_CARDINALITY_LIMIT,
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
from theater.observability.metrics import MetricBridge, MetricKind, MetricSpec
from theater.pricing import estimate_cost_usd
from theater.trajectory import (
    CostProvenance,
    TimingProvenance,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryStatus,
)

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
_DIRECT_TIMING_PROVENANCE = frozenset({TimingProvenance.SOURCE, TimingProvenance.OBSERVED})
_RESULTS = {
    TrajectoryStatus.COMPLETED: AGENT_RESULT_SUCCESS,
    TrajectoryStatus.ERROR: AGENT_RESULT_ERROR,
    TrajectoryStatus.TIMEOUT: AGENT_RESULT_TIMEOUT,
    TrajectoryStatus.CANCELLED: AGENT_RESULT_CANCELLED,
    TrajectoryStatus.INTERRUPTED: AGENT_RESULT_INTERRUPTED,
}
_COST_PROVENANCE = frozenset(provenance.value for provenance in CostProvenance)

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


@dataclass(slots=True)
class _ParticipantState:
    source_epoch: str
    emitted: OrderedDict[tuple[str, str], None] = field(default_factory=OrderedDict)


class AgentTelemetry:
    """Projects bounded process telemetry without retaining trajectory records."""

    def __init__(self, store: Any, bridge: MetricBridge) -> None:
        self._store = store
        self._bridge = bridge
        self._participants: OrderedDict[str, _ParticipantState] = OrderedDict()
        self._models: set[str] = set()
        self._tools: set[str] = set()

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
        state = self._state_for(participant_id, source_epoch)
        harness = _label(participant.harness)
        self._record_usage(harness, new_usage_events)
        records = project_batch(batch, participant_id=participant_id, source_epoch=source_epoch)
        for record in records:
            self._record_request(state, harness, record)
            self._record_tool(state, harness, record)
            self._record_failure(state, harness, record)

    def discard(self, participant_id: str) -> None:
        """Forget deduplication state for a discarded participant stream."""
        self._participants.pop(participant_id, None)

    def _state_for(self, participant_id: str, source_epoch: str) -> _ParticipantState:
        state = self._participants.get(participant_id)
        if state is None or state.source_epoch != source_epoch:
            state = _ParticipantState(source_epoch)
            self._participants[participant_id] = state
        self._participants.move_to_end(participant_id)
        while len(self._participants) > AGENT_TELEMETRY_PARTICIPANT_STATE_LIMIT:
            self._participants.popitem(last=False)
        return state

    def _record_usage(self, harness: str, events: tuple[Event, ...]) -> None:
        for event in events:
            usage = event.usage
            if usage is None:
                continue
            model = self._model_label(usage.model)
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
                        _spec(AGENT_TOKENS_METRIC),
                        count,
                        {"harness": harness, "model": model, "kind": kind},
                    )
            cost, provenance = _usage_cost(usage)
            if cost is not None:
                self._bridge.observe(
                    _spec(AGENT_COST_METRIC),
                    cost,
                    {"harness": harness, "model": model, "provenance": provenance},
                )

    def _record_request(
        self,
        state: _ParticipantState,
        harness: str,
        record: TrajectoryRecord,
    ) -> None:
        if (
            record.lane is not TrajectoryLane.MODEL
            or record.kind in {TrajectoryKind.CONTEXT, TrajectoryKind.SYSTEM}
            or record.status not in _TERMINAL_STATUSES
            or not _has_direct_timing(record)
        ):
            return
        assert record.timing is not None
        result = _RESULTS[record.status]
        model = self._model_label(record.usage.model if record.usage is not None else None)
        attributes = {
            "harness": harness,
            "model": model,
            "result": result,
            "timing_provenance": record.timing.provenance.value,
        }
        if record.timing.duration_ms is not None:
            self._observe_record(
                state,
                _spec(AGENT_REQUEST_DURATION_METRIC),
                record,
                record.timing.duration_ms,
                attributes,
            )
        ttft_ms = record.timing.ttft_ms
        if ttft_ms is not None:
            self._observe_record(
                state,
                _spec(AGENT_REQUEST_TTFT_METRIC),
                record,
                ttft_ms,
                attributes,
            )

    def _record_tool(
        self,
        state: _ParticipantState,
        harness: str,
        record: TrajectoryRecord,
    ) -> None:
        if (
            record.kind not in {TrajectoryKind.TOOL_CALL, TrajectoryKind.TOOL_RESULT}
            or record.status not in _TERMINAL_STATUSES
            or not _has_direct_timing(record)
        ):
            return
        assert record.timing is not None
        if record.timing.duration_ms is None:
            return
        self._observe_record(
            state,
            _spec(AGENT_TOOL_DURATION_METRIC),
            record,
            record.timing.duration_ms,
            {
                "harness": harness,
                "tool": self._tool_label(record),
                "result": _RESULTS[record.status],
                "timing_provenance": record.timing.provenance.value,
            },
        )

    def _record_failure(
        self,
        state: _ParticipantState,
        harness: str,
        record: TrajectoryRecord,
    ) -> None:
        if record.status not in _TERMINAL_STATUSES:
            return
        if record.failure is None and record.status not in _FAILURE_STATUSES:
            return
        category = _failure_category(record)
        self._observe_record(
            state,
            _spec(AGENT_FAILURES_METRIC),
            record,
            1,
            {"harness": harness, "category": _label(category)},
        )

    def _model_label(self, value: object) -> str:
        return self._bounded_cardinality_label(
            value,
            self._models,
            AGENT_TELEMETRY_MODEL_CARDINALITY_LIMIT,
        )

    def _tool_label(self, record: TrajectoryRecord) -> str:
        mcp_tool = _label(getattr(record, "mcp_tool", None))
        if mcp_tool != AGENT_TELEMETRY_UNKNOWN_LABEL:
            mcp_server = _label(getattr(record, "mcp_server", None))
            value = (
                f"{mcp_server}/{mcp_tool}"
                if mcp_server != AGENT_TELEMETRY_UNKNOWN_LABEL
                else mcp_tool
            )
            return self._bounded_cardinality_label(
                value,
                self._tools,
                AGENT_TELEMETRY_TOOL_CARDINALITY_LIMIT,
            )
        for detail in record.details:
            if detail.name == "tool":
                return self._bounded_cardinality_label(
                    detail.preview.text,
                    self._tools,
                    AGENT_TELEMETRY_TOOL_CARDINALITY_LIMIT,
                )
        return AGENT_TELEMETRY_UNKNOWN_LABEL

    @staticmethod
    def _bounded_cardinality_label(value: object, seen: set[str], limit: int) -> str:
        label = _label(value)
        if label in {AGENT_TELEMETRY_UNKNOWN_LABEL, AGENT_TELEMETRY_OTHER_LABEL}:
            return label
        if label in seen:
            return label
        if len(seen) >= limit:
            return AGENT_TELEMETRY_OTHER_LABEL
        seen.add(label)
        return label

    def _observe_record(
        self,
        state: _ParticipantState,
        spec: MetricSpec,
        record: TrajectoryRecord,
        value: float | int,
        attributes: dict[str, str],
    ) -> None:
        key = (spec.name, record.record_id)
        if key in state.emitted:
            return
        self._bridge.observe(spec, value, attributes)
        state.emitted[key] = None
        while len(state.emitted) > AGENT_TELEMETRY_EMITTED_SIGNAL_LIMIT:
            state.emitted.popitem(last=False)


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


def _spec(name: str) -> MetricSpec:
    return next(spec for spec in AGENT_METRIC_SPECS if spec.name == name)


def _has_direct_timing(record: TrajectoryRecord) -> bool:
    return (
        record.timing is not None
        and record.timing.provenance in _DIRECT_TIMING_PROVENANCE
        and (record.timing.duration_ms is not None or record.timing.ttft_ms is not None)
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
        provenance = _label(getattr(usage.cost_provenance, "value", usage.cost_provenance))
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


def _label(value: object) -> str:
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


__all__ = ["AGENT_METRIC_SPECS", "AgentTelemetry", "create_agent_telemetry"]
