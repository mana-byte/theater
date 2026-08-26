"""Agent trajectory structured-log and completed-span telemetry tests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    TraceFlags,
    TraceState,
    get_current_span,
    set_span_in_context,
)

import theater.daemon.trajectory.telemetry.labels as telemetry_labels
import theater.daemon.trajectory.telemetry.logs as telemetry_logs
import theater.daemon.trajectory.telemetry.state as telemetry_state
from theater.daemon.trajectory.telemetry import AgentTelemetry
from theater.daemon.trajectory.telemetry.state import AgentTelemetryState
from theater.harness.contracts.source import Attachment, Batch
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.models import Participant
from theater.observability.catalog import TraceKind
from theater.trajectory import (
    DetailField,
    Timing,
    TimingProvenance,
    TrajectoryFailure,
    TrajectoryFailureCategory,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryStatus,
    TrajectoryUsage,
)


@dataclass
class _Store:
    participant: Participant

    def get_participant(self, participant_id: str) -> Participant | None:
        return self.participant if participant_id == self.participant.id else None


@dataclass
class _MetricBridge:
    active: bool = True
    fail: bool = False
    observations: list[object] = field(default_factory=list)

    def observe(self, *args: object) -> None:
        if self.fail:
            raise RuntimeError("metric failure")
        self.observations.append(args)


@dataclass
class _SignalBridge:
    active: bool = True
    fail_log: bool = False
    fail_span: bool = False
    log_result: bool = True
    logs: list[dict[str, object]] = field(default_factory=list)
    spans: list[dict[str, object]] = field(default_factory=list)
    _next_span_id: int = 1

    def emit_log(self, event_name: str, **kwargs: object) -> bool:
        if self.fail_log:
            raise RuntimeError("log failure")
        if not self.log_result:
            return False
        self.logs.append({"event_name": event_name, **kwargs})
        return True

    def emit_span(self, name: str, **kwargs: object) -> object | None:
        if self.fail_span:
            raise RuntimeError("span failure")
        span_context = SpanContext(
            trace_id=1,
            span_id=self._next_span_id,
            is_remote=False,
            trace_flags=TraceFlags(1),
            trace_state=TraceState(),
        )
        self._next_span_id += 1
        context = set_span_in_context(NonRecordingSpan(span_context))
        self.spans.append({"name": name, "context": context, **kwargs})
        return context


def _participant() -> Participant:
    return Participant(
        id="agent",
        harness="codex",
        parent_id="parent",
        session_id="session",
        transcript_location="one.jsonl",
    )


def _fact(
    native_id: str,
    *,
    kind: TrajectoryKind = TrajectoryKind.ASSISTANT,
    lane: TrajectoryLane = TrajectoryLane.MODEL,
    status: TrajectoryStatus = TrajectoryStatus.COMPLETED,
    revision: int = 0,
    timing: Timing | None = None,
    usage: TrajectoryUsage | None = None,
    failure: TrajectoryFailure | None = None,
    request_id: str | None = None,
    call_id: str | None = None,
    parent_call_id: str | None = None,
    details: tuple[DetailField, ...] = (),
    summary: str = "private summary",
    raw_index: int = 1,
) -> TrajectoryFact:
    return TrajectoryFact(
        native_id=native_id,
        kind=kind,
        lane=lane,
        status=status,
        revision=revision,
        timing=timing,
        usage=usage,
        failure=failure,
        request_id=request_id,
        call_id=call_id,
        parent_call_id=parent_call_id,
        details=details,
        summary=summary,
        raw_index=raw_index,
    )


def test_logs_dedupe_revisions_reset_and_keep_default_body_private() -> None:
    signals = _SignalBridge()
    telemetry = AgentTelemetry(
        _Store(_participant()),
        signal_bridge=signals,
        metrics_enabled=False,
        logs_enabled=True,
    )
    running = _fact(
        "record",
        status=TrajectoryStatus.RUNNING,
        timing=Timing(start=1.25, provenance=TimingProvenance.SOURCE),
    )
    completed = _fact(
        "record",
        revision=1,
        timing=Timing(end=2.5, provenance=TimingProvenance.SOURCE),
    )

    telemetry.record_batch("agent", Batch(trajectory=(running,)), ())
    telemetry.record_batch("agent", Batch(trajectory=(running,)), ())
    telemetry.record_batch("agent", Batch(trajectory=(completed,)), ())
    telemetry.record_batch("agent", Batch(trajectory=(running,)), ())

    assert len(signals.logs) == 2
    state = next(iter(telemetry._state._participants.values()))
    assert next(iter(state.records.values())).revision == 1
    first, second = signals.logs
    assert first["body"] == "assistant:running"
    assert "private summary" not in str(first["body"])
    assert first["timestamp_ns"] == 1_250_000_000
    assert second["timestamp_ns"] == 2_500_000_000
    assert second["severity_text"] == "INFO"
    attributes = second["attributes"]
    assert isinstance(attributes, dict)
    assert attributes["theater.agent.trajectory.schema.version"] == 1
    assert attributes["theater.agent.parent.id"] == "parent"
    assert attributes["theater.agent.session.id"] == "session"
    assert all(type(value) in {str, int, float, bool} for value in attributes.values())

    telemetry.record_batch(
        "agent", Batch(trajectory=(running,), attached=Attachment("two.jsonl")), ()
    )
    telemetry.discard("agent")
    telemetry.record_batch(
        "agent", Batch(trajectory=(running,), attached=Attachment("two.jsonl")), ()
    )
    assert len(signals.logs) == 4


def test_opt_in_log_body_is_capped_without_exporting_content_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telemetry_logs, "AGENT_LOG_BODY_MAX_BYTES", 1_024)
    signals = _SignalBridge()
    telemetry = AgentTelemetry(
        _Store(_participant()),
        signal_bridge=signals,
        metrics_enabled=False,
        logs_enabled=True,
        include_log_content=True,
    )
    telemetry.record_batch("agent", Batch(trajectory=(_fact("body", summary="x" * 1_000),)), ())

    log = signals.logs[0]
    assert len(str(log["body"]).encode("utf-8")) <= 1_024
    assert json.loads(str(log["body"]))["record_id"].endswith(":body")
    attributes = log["attributes"]
    assert isinstance(attributes, dict)
    assert attributes["theater.agent.log.body.truncated"] is True
    assert attributes["theater.agent.log.body.omitted_bytes"] > 0


def test_spans_have_honest_intervals_parents_and_log_correlation() -> None:
    signals = _SignalBridge()
    telemetry = AgentTelemetry(
        _Store(_participant()),
        signal_bridge=signals,
        metrics_enabled=False,
        logs_enabled=True,
        spans_enabled=True,
    )
    request = _fact(
        "request",
        request_id="request-source",
        usage=TrajectoryUsage(model="gpt", provider="openai"),
        timing=Timing(start=1.0, end=4.0, duration_ms=3_000, provenance=TimingProvenance.SOURCE),
    )
    parent_call = _fact(
        "parent",
        kind=TrajectoryKind.TOOL_CALL,
        lane=TrajectoryLane.TOOLS,
        status=TrajectoryStatus.RUNNING,
        request_id="request-source",
        call_id="parent-call",
        timing=Timing(start=1.5, provenance=TimingProvenance.SOURCE),
        details=(DetailField.from_text("tool", "shell"),),
        raw_index=2,
    )
    parent_result = _fact(
        "parent-result",
        kind=TrajectoryKind.TOOL_RESULT,
        lane=TrajectoryLane.TOOLS,
        request_id="request-source",
        call_id="parent-call",
        timing=Timing(end=3.5, provenance=TimingProvenance.SOURCE),
        raw_index=3,
    )
    child_call = _fact(
        "child",
        kind=TrajectoryKind.TOOL_CALL,
        lane=TrajectoryLane.TOOLS,
        status=TrajectoryStatus.RUNNING,
        request_id="request-source",
        call_id="child-call",
        parent_call_id="parent-call",
        timing=Timing(start=2.5, provenance=TimingProvenance.SOURCE),
        details=(DetailField.from_text("tool", "nested"),),
        raw_index=4,
    )
    child_result = _fact(
        "child-result",
        kind=TrajectoryKind.TOOL_RESULT,
        lane=TrajectoryLane.TOOLS,
        request_id="request-source",
        call_id="child-call",
        timing=Timing(end=2.75, provenance=TimingProvenance.SOURCE),
        raw_index=5,
    )

    telemetry.record_batch(
        "agent",
        Batch(trajectory=(request, parent_call, parent_result, child_call, child_result)),
        (),
    )

    assert [span["name"] for span in signals.spans] == [
        "agent.request",
        "agent.tool",
        "agent.tool",
    ]
    request_span, parent_span, child_span = signals.spans
    assert request_span["start_time_ns"] == 1_000_000_000
    assert request_span["end_time_ns"] == 4_000_000_000
    assert parent_span["start_time_ns"] == 1_500_000_000
    assert parent_span["end_time_ns"] == 3_500_000_000
    assert child_span["start_time_ns"] == 2_500_000_000
    assert child_span["end_time_ns"] == 2_750_000_000
    assert request_span["kind"] is TraceKind.CLIENT
    assert parent_span["kind"] is TraceKind.INTERNAL
    assert parent_span["parent_context"] is not request_span["context"]
    assert (
        parent_span["links"][0].span_id
        == get_current_span(request_span["context"]).get_span_context().span_id
    )
    assert child_span["parent_context"] is parent_span["context"]
    assert (
        child_span["links"][0].span_id
        == get_current_span(request_span["context"]).get_span_context().span_id
    )
    assert request_span["attributes"]["theater.agent.model"] == "gpt"
    assert parent_span["attributes"]["theater.agent.tool"] == "shell"
    assert get_current_span(request_span["parent_context"]).get_span_context().is_valid is False
    by_id = {
        log["attributes"]["theater.agent.record.id"]: log
        for log in signals.logs
        if isinstance(log["attributes"], dict)
    }
    assert (
        by_id[next(key for key in by_id if key.endswith(":request"))]["context"]
        is request_span["context"]
    )


def test_duration_only_spans_skip_and_terminal_metric_counts_dedupe() -> None:
    metrics = _MetricBridge()
    signals = _SignalBridge()
    telemetry = AgentTelemetry(_Store(_participant()), metrics, signals, spans_enabled=True)
    request = _fact(
        "request",
        request_id="request-source",
        usage=TrajectoryUsage(model="gpt"),
        timing=Timing(duration_ms=50, provenance=TimingProvenance.SOURCE),
    )
    tool = _fact(
        "tool",
        kind=TrajectoryKind.TOOL_RESULT,
        lane=TrajectoryLane.TOOLS,
        call_id="tool-call",
        timing=Timing(duration_ms=25, provenance=TimingProvenance.SOURCE),
        details=(DetailField.from_text("tool", "shell"),),
        raw_index=2,
    )
    batch = Batch(trajectory=(request, tool))

    telemetry.record_batch("agent", batch, ())
    telemetry.record_batch("agent", batch, ())

    names = [observation[0].name for observation in metrics.observations]
    assert names.count("theater.agent.requests") == 1
    assert names.count("theater.agent.tool.calls") == 1
    assert signals.spans == []


def test_signal_failures_are_isolated() -> None:
    fact = _fact(
        "record",
        timing=Timing(start=1, end=2, duration_ms=1_000, provenance=TimingProvenance.SOURCE),
    )
    signals = _SignalBridge()
    telemetry = AgentTelemetry(
        _Store(_participant()),
        _MetricBridge(fail=True),
        signals,
        logs_enabled=True,
        spans_enabled=True,
    )
    telemetry.record_batch("agent", Batch(trajectory=(fact,)), ())
    assert signals.logs and signals.spans

    log_failure = _SignalBridge(fail_log=True)
    telemetry = AgentTelemetry(
        _Store(_participant()),
        signal_bridge=log_failure,
        metrics_enabled=False,
        logs_enabled=True,
        spans_enabled=True,
    )
    telemetry.record_batch("agent", Batch(trajectory=(fact,)), ())
    assert log_failure.spans

    span_failure = _SignalBridge(fail_span=True)
    telemetry = AgentTelemetry(
        _Store(_participant()),
        signal_bridge=span_failure,
        metrics_enabled=False,
        logs_enabled=True,
        spans_enabled=True,
    )
    telemetry.record_batch("agent", Batch(trajectory=(fact,)), ())
    assert span_failure.logs


def test_emission_state_lru_budgets_are_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telemetry_state, "AGENT_TELEMETRY_EMITTED_SIGNAL_LIMIT", 2)
    monkeypatch.setattr(telemetry_state, "AGENT_TELEMETRY_LOG_REVISION_LIMIT", 2)
    state = AgentTelemetryState()
    participant = state.for_participant("agent", "epoch")
    state.remember(participant, "metric", "a")
    state.remember(participant, "metric", "b")
    assert state.contains(participant, "metric", "a")
    state.remember(participant, "metric", "c")
    assert list(participant.emitted) == [("metric", "a"), ("metric", "c")]
    state.remember_log(participant, "a", 1)
    state.remember_log(participant, "b", 1)
    state.remember_log(participant, "c", 1)
    assert list(participant.log_revisions) == ["b", "c"]
    assert len(participant.emitted) == 2


def test_split_call_and_result_batches_close_from_retained_snapshot() -> None:
    metrics = _MetricBridge()
    signals = _SignalBridge()
    telemetry = AgentTelemetry(
        _Store(_participant()),
        metrics,
        signals,
        logs_enabled=True,
        spans_enabled=True,
    )
    call = _fact(
        "call",
        kind=TrajectoryKind.TOOL_CALL,
        lane=TrajectoryLane.TOOLS,
        status=TrajectoryStatus.RUNNING,
        call_id="call-id",
        timing=Timing(start=10, provenance=TimingProvenance.SOURCE),
        details=(DetailField.from_text("tool", "shell"),),
    )
    result = _fact(
        "result",
        kind=TrajectoryKind.TOOL_RESULT,
        lane=TrajectoryLane.TOOLS,
        call_id="call-id",
        timing=Timing(end=12, provenance=TimingProvenance.SOURCE),
        raw_index=2,
    )

    telemetry.record_batch("agent", Batch(trajectory=(call,)), ())
    telemetry.record_batch("agent", Batch(trajectory=(result,)), ())

    observations = [observation[0].name for observation in metrics.observations]
    assert observations.count("theater.agent.tool.calls") == 1
    assert observations.count("theater.agent.tool.duration") == 1
    tool_span = next(span for span in signals.spans if span["name"] == "agent.tool")
    assert tool_span["start_time_ns"] == 10_000_000_000
    assert tool_span["end_time_ns"] == 12_000_000_000


def test_failed_log_is_retried_only_until_a_successful_highest_revision() -> None:
    signals = _SignalBridge(log_result=False)
    telemetry = AgentTelemetry(
        _Store(_participant()),
        signal_bridge=signals,
        metrics_enabled=False,
        logs_enabled=True,
    )
    record = _fact("record")

    telemetry.record_batch("agent", Batch(trajectory=(record,)), ())
    signals.log_result = True
    telemetry.record_batch("agent", Batch(trajectory=(record,)), ())
    telemetry.record_batch("agent", Batch(trajectory=(record,)), ())

    assert len(signals.logs) == 1


def test_higher_revision_adds_timing_and_span_without_recounting_terminal_request() -> None:
    metrics = _MetricBridge()
    signals = _SignalBridge()
    telemetry = AgentTelemetry(_Store(_participant()), metrics, signals, spans_enabled=True)
    untimed = _fact(
        "request",
        request_id="request-id",
        usage=TrajectoryUsage(model="gpt"),
    )
    timed = _fact(
        "request",
        revision=1,
        request_id="request-id",
        usage=TrajectoryUsage(model="gpt"),
        timing=Timing(start=1, end=3, duration_ms=2_000, provenance=TimingProvenance.SOURCE),
    )

    telemetry.record_batch("agent", Batch(trajectory=(untimed,)), ())
    telemetry.record_batch("agent", Batch(trajectory=(timed,)), ())

    names = [observation[0].name for observation in metrics.observations]
    assert names.count("theater.agent.requests") == 1
    assert names.count("theater.agent.request.duration") == 1
    assert [span["name"] for span in signals.spans] == ["agent.request"]


def test_snapshot_bounds_keep_epochs_independent_and_drop_record_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telemetry_state, "AGENT_TELEMETRY_RECORD_SNAPSHOT_LIMIT", 2)
    state = AgentTelemetryState()
    first_epoch = state.for_participant("agent", "one")
    second_epoch = state.for_participant("agent", "two")
    assert first_epoch is state.for_participant("agent", "one")
    assert second_epoch is state.for_participant("agent", "two")
    assert first_epoch is not second_epoch

    telemetry = AgentTelemetry(
        _Store(_participant()),
        signal_bridge=_SignalBridge(),
        metrics_enabled=False,
        logs_enabled=True,
    )
    for native_id in ("one", "two", "three"):
        telemetry.record_batch(
            "agent",
            Batch(trajectory=(_fact(native_id, summary=f"secret-{native_id}"),)),
            (),
        )
    active = next(iter(telemetry._state._participants.values()))
    assert len(active.records) == 2
    snapshot = next(iter(active.records.values()))
    assert not hasattr(snapshot, "summary")
    assert not hasattr(snapshot, "details")
    assert not hasattr(snapshot, "failure_detail")


def test_default_log_metadata_excludes_all_record_content() -> None:
    signals = _SignalBridge()
    telemetry = AgentTelemetry(
        _Store(_participant()),
        signal_bridge=signals,
        metrics_enabled=False,
        logs_enabled=True,
    )
    sensitive = "super-secret-result /private/path raw-arguments"
    record = _fact(
        "private",
        summary=sensitive,
        failure=TrajectoryFailure(detail=sensitive),
        details=(
            DetailField.from_text("raw", sensitive),
            DetailField.from_text("path.read", "/private/path"),
        ),
    )

    telemetry.record_batch("agent", Batch(trajectory=(record,)), ())

    log = signals.logs[0]
    assert log["body"] == "assistant:completed"
    assert sensitive not in str(log["attributes"])
    assert "/private/path" not in str(log["attributes"])


def test_incompatible_nested_tool_interval_is_not_parented() -> None:
    signals = _SignalBridge()
    telemetry = AgentTelemetry(
        _Store(_participant()),
        signal_bridge=signals,
        metrics_enabled=False,
        spans_enabled=True,
    )
    parent = _fact(
        "parent",
        kind=TrajectoryKind.TOOL_CALL,
        lane=TrajectoryLane.TOOLS,
        status=TrajectoryStatus.RUNNING,
        call_id="parent-call",
        timing=Timing(start=1, provenance=TimingProvenance.SOURCE),
        details=(DetailField.from_text("tool", "parent"),),
    )
    parent_result = _fact(
        "parent-result",
        kind=TrajectoryKind.TOOL_RESULT,
        lane=TrajectoryLane.TOOLS,
        call_id="parent-call",
        timing=Timing(end=2, provenance=TimingProvenance.SOURCE),
        raw_index=2,
    )
    child = _fact(
        "child",
        kind=TrajectoryKind.TOOL_CALL,
        lane=TrajectoryLane.TOOLS,
        status=TrajectoryStatus.RUNNING,
        call_id="child-call",
        parent_call_id="parent-call",
        timing=Timing(start=3, provenance=TimingProvenance.SOURCE),
        details=(DetailField.from_text("tool", "child"),),
        raw_index=3,
    )
    child_result = _fact(
        "child-result",
        kind=TrajectoryKind.TOOL_RESULT,
        lane=TrajectoryLane.TOOLS,
        call_id="child-call",
        timing=Timing(end=4, provenance=TimingProvenance.SOURCE),
        raw_index=4,
    )

    telemetry.record_batch(
        "agent", Batch(trajectory=(parent, parent_result, child, child_result)), ()
    )

    parent_span, child_span = signals.spans
    assert child_span["parent_context"] is not parent_span["context"]


def test_completed_keyed_call_waits_for_result_before_final_tool_signals() -> None:
    metrics = _MetricBridge()
    signals = _SignalBridge()
    telemetry = AgentTelemetry(_Store(_participant()), metrics, signals, spans_enabled=True)
    call = _fact(
        "call",
        kind=TrajectoryKind.TOOL_CALL,
        lane=TrajectoryLane.TOOLS,
        call_id="call-id",
        timing=Timing(start=10, duration_ms=500, provenance=TimingProvenance.SOURCE),
        details=(DetailField.from_text("tool", "shell"),),
    )
    result = _fact(
        "result",
        kind=TrajectoryKind.TOOL_RESULT,
        lane=TrajectoryLane.TOOLS,
        call_id="call-id",
        timing=Timing(end=12, provenance=TimingProvenance.SOURCE),
        raw_index=2,
    )

    telemetry.record_batch("agent", Batch(trajectory=(call,)), ())
    assert "theater.agent.tool.calls" not in [item[0].name for item in metrics.observations]
    assert signals.spans == []

    telemetry.record_batch("agent", Batch(trajectory=(result,)), ())

    names = [item[0].name for item in metrics.observations]
    assert names.count("theater.agent.tool.calls") == 1
    assert names.count("theater.agent.tool.duration") == 1
    assert len(signals.spans) == 1
    assert signals.spans[0]["name"] == "agent.tool"
    assert signals.spans[0]["start_time_ns"] == 10_000_000_000
    assert signals.spans[0]["end_time_ns"] == 12_000_000_000


def test_unrelated_batch_does_not_replay_evicted_historical_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telemetry_state, "AGENT_TELEMETRY_EMITTED_SIGNAL_LIMIT", 2)
    metrics = _MetricBridge()
    signals = _SignalBridge()
    telemetry = AgentTelemetry(_Store(_participant()), metrics, signals, spans_enabled=True)
    historical = tuple(
        _fact(
            f"request-{index}",
            request_id=f"request-{index}",
            usage=TrajectoryUsage(model=f"model-{index}"),
            timing=Timing(
                start=float(index),
                end=float(index + 1),
                duration_ms=1_000,
                provenance=TimingProvenance.SOURCE,
            ),
            raw_index=index,
        )
        for index in range(3)
    )

    telemetry.record_batch("agent", Batch(trajectory=historical), ())
    telemetry.record_batch(
        "agent",
        Batch(trajectory=(_fact("unrelated", status=TrajectoryStatus.RUNNING, raw_index=4),)),
        (),
    )

    assert len(signals.spans) == 3
    names = [item[0].name for item in metrics.observations]
    assert names.count("theater.agent.requests") == 3


def test_cached_span_context_correlates_retries_and_higher_revisions() -> None:
    signals = _SignalBridge(log_result=False)
    telemetry = AgentTelemetry(
        _Store(_participant()),
        signal_bridge=signals,
        metrics_enabled=False,
        logs_enabled=True,
        spans_enabled=True,
    )
    initial = _fact(
        "request",
        request_id="request-id",
        timing=Timing(start=1, end=2, duration_ms=1_000, provenance=TimingProvenance.SOURCE),
    )
    revised = _fact(
        "request",
        revision=1,
        request_id="request-id",
        timing=Timing(start=1, end=3, duration_ms=2_000, provenance=TimingProvenance.SOURCE),
    )

    telemetry.record_batch("agent", Batch(trajectory=(initial,)), ())
    span_context = signals.spans[0]["context"]
    signals.log_result = True
    telemetry.record_batch("agent", Batch(trajectory=(initial,)), ())
    telemetry.record_batch("agent", Batch(trajectory=(revised,)), ())

    assert len(signals.spans) == 1
    assert len(signals.logs) == 2
    assert all(log["context"] is span_context for log in signals.logs)


def test_span_names_kinds_and_terminal_error_semantics() -> None:
    signals = _SignalBridge()
    telemetry = AgentTelemetry(
        _Store(_participant()),
        signal_bridge=signals,
        metrics_enabled=False,
        spans_enabled=True,
    )
    statuses = (
        TrajectoryStatus.ERROR,
        TrajectoryStatus.TIMEOUT,
        TrajectoryStatus.CANCELLED,
        TrajectoryStatus.INTERRUPTED,
    )
    requests = tuple(
        _fact(
            status.value,
            status=status,
            request_id=status.value,
            timing=Timing(
                start=float(index),
                end=float(index + 1),
                duration_ms=1_000,
                provenance=TimingProvenance.SOURCE,
            ),
            raw_index=index,
        )
        for index, status in enumerate(statuses, start=1)
    )
    tool_call = _fact(
        "tool-call",
        kind=TrajectoryKind.TOOL_CALL,
        lane=TrajectoryLane.TOOLS,
        status=TrajectoryStatus.RUNNING,
        call_id="tool-call",
        timing=Timing(start=10, provenance=TimingProvenance.SOURCE),
        details=(DetailField.from_text("tool", "shell"),),
        raw_index=10,
    )
    tool_result = _fact(
        "tool-result",
        kind=TrajectoryKind.TOOL_RESULT,
        lane=TrajectoryLane.TOOLS,
        call_id="tool-call",
        timing=Timing(end=11, provenance=TimingProvenance.SOURCE),
        failure=TrajectoryFailure(category=TrajectoryFailureCategory.TOOL),
        raw_index=11,
    )

    telemetry.record_batch("agent", Batch(trajectory=(*requests, tool_call, tool_result)), ())

    request_spans = [span for span in signals.spans if span["name"] == "agent.request"]
    assert [span["kind"] for span in request_spans] == [TraceKind.CLIENT] * len(statuses)
    assert [span["error"] for span in request_spans] == [True] * len(statuses)
    assert [span["error_type"] for span in request_spans] == [status.value for status in statuses]
    tool_span = next(span for span in signals.spans if span["name"] == "agent.tool")
    assert tool_span["kind"] is TraceKind.INTERNAL
    assert tool_span["error"] is True
    assert tool_span["error_type"] == "tool"


def test_spans_only_derive_missing_absolute_endpoint() -> None:
    signals = _SignalBridge()
    telemetry = AgentTelemetry(
        _Store(_participant()),
        signal_bridge=signals,
        metrics_enabled=False,
        spans_enabled=True,
    )
    partial = _fact(
        "partial",
        request_id="partial",
        timing=Timing(end=5, duration_ms=1_000, provenance=TimingProvenance.SOURCE),
    )
    duration_only = _fact(
        "duration-only",
        request_id="duration-only",
        timing=Timing(duration_ms=1_000, provenance=TimingProvenance.SOURCE),
        raw_index=2,
    )

    telemetry.record_batch("agent", Batch(trajectory=(partial, duration_only)), ())

    assert len(signals.spans) == 1
    assert signals.spans[0]["start_time_ns"] == 4_000_000_000
    assert signals.spans[0]["end_time_ns"] == 5_000_000_000


def test_span_values_do_not_use_metric_cardinality_other_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telemetry_labels, "AGENT_TELEMETRY_MODEL_CARDINALITY_LIMIT", 1)
    monkeypatch.setattr(telemetry_labels, "AGENT_TELEMETRY_TOOL_CARDINALITY_LIMIT", 1)
    metrics = _MetricBridge()
    signals = _SignalBridge()
    telemetry = AgentTelemetry(_Store(_participant()), metrics, signals, spans_enabled=True)
    requests = tuple(
        _fact(
            f"request-{model}",
            request_id=f"request-{model}",
            usage=TrajectoryUsage(model=model),
            timing=Timing(
                start=float(index),
                end=float(index + 1),
                duration_ms=1_000,
                provenance=TimingProvenance.SOURCE,
            ),
            raw_index=index,
        )
        for index, model in enumerate(("first", "second"), start=1)
    )
    tools = tuple(
        fact
        for index, tool in enumerate(("first-tool", "second-tool"), start=3)
        for fact in (
            _fact(
                f"{tool}-call",
                kind=TrajectoryKind.TOOL_CALL,
                lane=TrajectoryLane.TOOLS,
                status=TrajectoryStatus.RUNNING,
                call_id=tool,
                timing=Timing(start=float(index), provenance=TimingProvenance.SOURCE),
                details=(DetailField.from_text("tool", tool),),
                raw_index=index,
            ),
            _fact(
                f"{tool}-result",
                kind=TrajectoryKind.TOOL_RESULT,
                lane=TrajectoryLane.TOOLS,
                call_id=tool,
                timing=Timing(end=float(index + 1), provenance=TimingProvenance.SOURCE),
                raw_index=index + 10,
            ),
        )
    )

    telemetry.record_batch("agent", Batch(trajectory=(*requests, *tools)), ())

    request_metrics = [
        item[2]["model"]
        for item in metrics.observations
        if item[0].name == "theater.agent.requests"
    ]
    tool_metrics = [
        item[2]["tool"]
        for item in metrics.observations
        if item[0].name == "theater.agent.tool.calls"
    ]
    request_models = [
        span["attributes"]["theater.agent.model"]
        for span in signals.spans
        if span["name"] == "agent.request"
    ]
    tool_names = [
        span["attributes"]["theater.agent.tool"]
        for span in signals.spans
        if span["name"] == "agent.tool"
    ]
    assert request_metrics == ["first", "other"]
    assert tool_metrics == ["first-tool", "other"]
    assert request_models == ["first", "second"]
    assert tool_names == ["first-tool", "second-tool"]
