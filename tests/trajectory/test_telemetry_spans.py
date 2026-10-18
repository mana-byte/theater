"""Agent trajectory completed spans: intervals, parents, links and error semantics."""

from __future__ import annotations

import pytest
from opentelemetry.trace import (
    get_current_span,
)

import theater.daemon.trajectory.telemetry.labels as telemetry_labels
from tests.support.agent_telemetry import (
    SignalBridge,
    SignalMetricBridge,
    SignalStore,
    signal_fact,
    signal_participant,
)
from theater.daemon.trajectory.telemetry import AgentTelemetry
from theater.harness.contracts.source import Batch
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


def test_spans_have_honest_intervals_parents_and_log_correlation() -> None:
    signals = SignalBridge()
    telemetry = AgentTelemetry(
        SignalStore(signal_participant()),
        signal_bridge=signals,
        metrics_enabled=False,
        logs_enabled=True,
        spans_enabled=True,
    )
    request = signal_fact(
        "request",
        request_id="request-source",
        usage=TrajectoryUsage(model="gpt", provider="openai"),
        timing=Timing(start=1.0, end=4.0, duration_ms=3_000, provenance=TimingProvenance.SOURCE),
    )
    parent_call = signal_fact(
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
    parent_result = signal_fact(
        "parent-result",
        kind=TrajectoryKind.TOOL_RESULT,
        lane=TrajectoryLane.TOOLS,
        request_id="request-source",
        call_id="parent-call",
        timing=Timing(end=3.5, provenance=TimingProvenance.SOURCE),
        raw_index=3,
    )
    child_call = signal_fact(
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
    child_result = signal_fact(
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
    metrics = SignalMetricBridge()
    signals = SignalBridge()
    telemetry = AgentTelemetry(
        SignalStore(signal_participant()), metrics, signals, spans_enabled=True
    )
    request = signal_fact(
        "request",
        request_id="request-source",
        usage=TrajectoryUsage(model="gpt"),
        timing=Timing(duration_ms=50, provenance=TimingProvenance.SOURCE),
    )
    tool = signal_fact(
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


def test_split_call_and_result_batches_close_from_retained_snapshot() -> None:
    metrics = SignalMetricBridge()
    signals = SignalBridge()
    telemetry = AgentTelemetry(
        SignalStore(signal_participant()),
        metrics,
        signals,
        logs_enabled=True,
        spans_enabled=True,
    )
    call = signal_fact(
        "call",
        kind=TrajectoryKind.TOOL_CALL,
        lane=TrajectoryLane.TOOLS,
        status=TrajectoryStatus.RUNNING,
        call_id="call-id",
        timing=Timing(start=10, provenance=TimingProvenance.SOURCE),
        details=(DetailField.from_text("tool", "shell"),),
    )
    result = signal_fact(
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


def test_higher_revision_adds_timing_and_span_without_recounting_terminal_request() -> None:
    metrics = SignalMetricBridge()
    signals = SignalBridge()
    telemetry = AgentTelemetry(
        SignalStore(signal_participant()), metrics, signals, spans_enabled=True
    )
    untimed = signal_fact(
        "request",
        request_id="request-id",
        usage=TrajectoryUsage(model="gpt"),
    )
    timed = signal_fact(
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


def test_incompatible_nested_tool_interval_is_not_parented() -> None:
    signals = SignalBridge()
    telemetry = AgentTelemetry(
        SignalStore(signal_participant()),
        signal_bridge=signals,
        metrics_enabled=False,
        spans_enabled=True,
    )
    parent = signal_fact(
        "parent",
        kind=TrajectoryKind.TOOL_CALL,
        lane=TrajectoryLane.TOOLS,
        status=TrajectoryStatus.RUNNING,
        call_id="parent-call",
        timing=Timing(start=1, provenance=TimingProvenance.SOURCE),
        details=(DetailField.from_text("tool", "parent"),),
    )
    parent_result = signal_fact(
        "parent-result",
        kind=TrajectoryKind.TOOL_RESULT,
        lane=TrajectoryLane.TOOLS,
        call_id="parent-call",
        timing=Timing(end=2, provenance=TimingProvenance.SOURCE),
        raw_index=2,
    )
    child = signal_fact(
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
    child_result = signal_fact(
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
    metrics = SignalMetricBridge()
    signals = SignalBridge()
    telemetry = AgentTelemetry(
        SignalStore(signal_participant()), metrics, signals, spans_enabled=True
    )
    call = signal_fact(
        "call",
        kind=TrajectoryKind.TOOL_CALL,
        lane=TrajectoryLane.TOOLS,
        call_id="call-id",
        timing=Timing(start=10, duration_ms=500, provenance=TimingProvenance.SOURCE),
        details=(DetailField.from_text("tool", "shell"),),
    )
    result = signal_fact(
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


def test_split_request_and_tool_batches_keep_request_span_link() -> None:
    signals = SignalBridge()
    telemetry = AgentTelemetry(
        SignalStore(signal_participant()),
        signal_bridge=signals,
        metrics_enabled=False,
        spans_enabled=True,
    )
    request = signal_fact(
        "request",
        request_id="request-id",
        timing=Timing(start=1, end=4, duration_ms=3_000, provenance=TimingProvenance.SOURCE),
    )
    call = signal_fact(
        "call",
        kind=TrajectoryKind.TOOL_CALL,
        lane=TrajectoryLane.TOOLS,
        status=TrajectoryStatus.RUNNING,
        request_id="request-id",
        call_id="call-id",
        timing=Timing(start=2, provenance=TimingProvenance.SOURCE),
        details=(DetailField.from_text("tool", "shell"),),
        raw_index=2,
    )
    result = signal_fact(
        "result",
        kind=TrajectoryKind.TOOL_RESULT,
        lane=TrajectoryLane.TOOLS,
        request_id="request-id",
        call_id="call-id",
        timing=Timing(end=3, provenance=TimingProvenance.SOURCE),
        raw_index=3,
    )

    telemetry.record_batch("agent", Batch(trajectory=(request,)), ())
    telemetry.record_batch("agent", Batch(trajectory=(call,)), ())
    telemetry.record_batch("agent", Batch(trajectory=(result,)), ())

    request_span, tool_span = signals.spans
    request_context = get_current_span(request_span["context"]).get_span_context()
    assert tool_span["links"][0].trace_id == request_context.trace_id
    assert tool_span["links"][0].span_id == request_context.span_id


def test_cached_span_context_correlates_retries_and_higher_revisions() -> None:
    signals = SignalBridge(log_result=False)
    telemetry = AgentTelemetry(
        SignalStore(signal_participant()),
        signal_bridge=signals,
        metrics_enabled=False,
        logs_enabled=True,
        spans_enabled=True,
    )
    initial = signal_fact(
        "request",
        request_id="request-id",
        timing=Timing(start=1, end=2, duration_ms=1_000, provenance=TimingProvenance.SOURCE),
    )
    revised = signal_fact(
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
    signals = SignalBridge()
    telemetry = AgentTelemetry(
        SignalStore(signal_participant()),
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
        signal_fact(
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
    tool_call = signal_fact(
        "tool-call",
        kind=TrajectoryKind.TOOL_CALL,
        lane=TrajectoryLane.TOOLS,
        status=TrajectoryStatus.RUNNING,
        call_id="tool-call",
        timing=Timing(start=10, provenance=TimingProvenance.SOURCE),
        details=(DetailField.from_text("tool", "shell"),),
        raw_index=10,
    )
    tool_result = signal_fact(
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
    signals = SignalBridge()
    telemetry = AgentTelemetry(
        SignalStore(signal_participant()),
        signal_bridge=signals,
        metrics_enabled=False,
        spans_enabled=True,
    )
    partial = signal_fact(
        "partial",
        request_id="partial",
        timing=Timing(end=5, duration_ms=1_000, provenance=TimingProvenance.SOURCE),
    )
    duration_only = signal_fact(
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
    metrics = SignalMetricBridge()
    signals = SignalBridge()
    telemetry = AgentTelemetry(
        SignalStore(signal_participant()), metrics, signals, spans_enabled=True
    )
    requests = tuple(
        signal_fact(
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
            signal_fact(
                f"{tool}-call",
                kind=TrajectoryKind.TOOL_CALL,
                lane=TrajectoryLane.TOOLS,
                status=TrajectoryStatus.RUNNING,
                call_id=tool,
                timing=Timing(start=float(index), provenance=TimingProvenance.SOURCE),
                details=(DetailField.from_text("tool", tool),),
                raw_index=index,
            ),
            signal_fact(
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
