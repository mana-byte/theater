"""Agent trajectory structured logs: dedupe, bounds, replay and failure isolation."""

from __future__ import annotations

import json

import pytest

import theater.daemon.trajectory.telemetry.logs as telemetry_logs
import theater.daemon.trajectory.telemetry.state as telemetry_state
from tests.support.agent_telemetry import (
    SignalBridge,
    SignalMetricBridge,
    SignalStore,
    signal_fact,
    signal_participant,
)
from theater.daemon.trajectory.telemetry import AgentTelemetry
from theater.daemon.trajectory.telemetry.state import AgentTelemetryState
from theater.harness.contracts.source import Attachment, Batch
from theater.trajectory import (
    DetailField,
    Timing,
    TimingProvenance,
    TrajectoryFailure,
    TrajectoryStatus,
    TrajectoryUsage,
)


def test_logs_dedupe_revisions_reset_and_keep_default_body_private() -> None:
    signals = SignalBridge()
    telemetry = AgentTelemetry(
        SignalStore(signal_participant()),
        signal_bridge=signals,
        metrics_enabled=False,
        logs_enabled=True,
    )
    running = signal_fact(
        "record",
        status=TrajectoryStatus.RUNNING,
        timing=Timing(start=1.25, provenance=TimingProvenance.SOURCE),
    )
    completed = signal_fact(
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
    signals = SignalBridge()
    telemetry = AgentTelemetry(
        SignalStore(signal_participant()),
        signal_bridge=signals,
        metrics_enabled=False,
        logs_enabled=True,
        include_log_content=True,
    )
    telemetry.record_batch(
        "agent", Batch(trajectory=(signal_fact("body", summary="x" * 1_000),)), ()
    )

    log = signals.logs[0]
    assert len(str(log["body"]).encode("utf-8")) <= 1_024
    assert json.loads(str(log["body"]))["record_id"].endswith(":body")
    attributes = log["attributes"]
    assert isinstance(attributes, dict)
    assert attributes["theater.agent.log.body.truncated"] is True
    assert attributes["theater.agent.log.body.omitted_bytes"] > 0


def test_signal_failures_are_isolated() -> None:
    fact = signal_fact(
        "record",
        timing=Timing(start=1, end=2, duration_ms=1_000, provenance=TimingProvenance.SOURCE),
    )
    signals = SignalBridge()
    telemetry = AgentTelemetry(
        SignalStore(signal_participant()),
        SignalMetricBridge(fail=True),
        signals,
        logs_enabled=True,
        spans_enabled=True,
    )
    telemetry.record_batch("agent", Batch(trajectory=(fact,)), ())
    assert signals.logs and signals.spans

    log_failure = SignalBridge(fail_log=True)
    telemetry = AgentTelemetry(
        SignalStore(signal_participant()),
        signal_bridge=log_failure,
        metrics_enabled=False,
        logs_enabled=True,
        spans_enabled=True,
    )
    telemetry.record_batch("agent", Batch(trajectory=(fact,)), ())
    assert log_failure.spans

    span_failure = SignalBridge(fail_span=True)
    telemetry = AgentTelemetry(
        SignalStore(signal_participant()),
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


def test_failed_log_is_retried_only_until_a_successful_highest_revision() -> None:
    signals = SignalBridge(log_result=False)
    telemetry = AgentTelemetry(
        SignalStore(signal_participant()),
        signal_bridge=signals,
        metrics_enabled=False,
        logs_enabled=True,
    )
    record = signal_fact("record")

    telemetry.record_batch("agent", Batch(trajectory=(record,)), ())
    signals.log_result = True
    telemetry.record_batch("agent", Batch(trajectory=(record,)), ())
    telemetry.record_batch("agent", Batch(trajectory=(record,)), ())

    assert len(signals.logs) == 1


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
        SignalStore(signal_participant()),
        signal_bridge=SignalBridge(),
        metrics_enabled=False,
        logs_enabled=True,
    )
    for native_id in ("one", "two", "three"):
        telemetry.record_batch(
            "agent",
            Batch(trajectory=(signal_fact(native_id, summary=f"secret-{native_id}"),)),
            (),
        )
    active = next(iter(telemetry._state._participants.values()))
    assert len(active.records) == 2
    snapshot = next(iter(active.records.values()))
    assert not hasattr(snapshot, "summary")
    assert not hasattr(snapshot, "details")
    assert not hasattr(snapshot, "failure_detail")


def test_default_log_metadata_excludes_all_record_content() -> None:
    signals = SignalBridge()
    telemetry = AgentTelemetry(
        SignalStore(signal_participant()),
        signal_bridge=signals,
        metrics_enabled=False,
        logs_enabled=True,
    )
    sensitive = "super-secret-result /private/path raw-arguments"
    record = signal_fact(
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


def test_unrelated_batch_does_not_replay_evicted_historical_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telemetry_state, "AGENT_TELEMETRY_EMITTED_SIGNAL_LIMIT", 2)
    metrics = SignalMetricBridge()
    signals = SignalBridge()
    telemetry = AgentTelemetry(
        SignalStore(signal_participant()), metrics, signals, spans_enabled=True
    )
    historical = tuple(
        signal_fact(
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
        Batch(trajectory=(signal_fact("unrelated", status=TrajectoryStatus.RUNNING, raw_index=4),)),
        (),
    )

    assert len(signals.spans) == 3
    names = [item[0].name for item in metrics.observations]
    assert names.count("theater.agent.requests") == 3


def test_snapshot_bound_does_not_drop_current_batch_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telemetry_state, "AGENT_TELEMETRY_RECORD_SNAPSHOT_LIMIT", 2)
    metrics = SignalMetricBridge()
    signals = SignalBridge()
    telemetry = AgentTelemetry(
        SignalStore(signal_participant()),
        metrics,
        signals,
        logs_enabled=True,
        spans_enabled=True,
    )
    records = tuple(
        signal_fact(
            f"request-{index}",
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

    telemetry.record_batch("agent", Batch(trajectory=records), ())

    assert len(signals.logs) == 3
    assert len(signals.spans) == 3
    names = [item[0].name for item in metrics.observations]
    assert names.count("theater.agent.requests") == 3
    state = next(iter(telemetry._state._participants.values()))
    assert len(state.records) == 2
