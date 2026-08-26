"""Agent telemetry projection tests."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from theater.constants.observability import (
    AGENT_COST_METRIC,
    AGENT_FAILURES_METRIC,
    AGENT_REQUEST_DURATION_METRIC,
    AGENT_REQUEST_TTFT_METRIC,
    AGENT_TELEMETRY_EMITTED_SIGNAL_LIMIT,
    AGENT_TELEMETRY_OTHER_LABEL,
    AGENT_TELEMETRY_PARTICIPANT_STATE_LIMIT,
    AGENT_TELEMETRY_UNKNOWN_LABEL,
    AGENT_TOKENS_METRIC,
    AGENT_TOOL_DURATION_METRIC,
)
from theater.daemon.trajectory import telemetry
from theater.daemon.trajectory.telemetry import (
    AGENT_METRIC_SPECS,
    AgentTelemetry,
    create_agent_telemetry,
)
from theater.harness.contracts.events import Event, EventKind, TokenUsage
from theater.harness.contracts.source import Attachment, Batch
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.models import Participant
from theater.observability.metrics import MetricKind, MetricSpec
from theater.trajectory import (
    DetailField,
    Timing,
    TimingProvenance,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryStatus,
    TrajectoryUsage,
)

Observation = tuple[MetricSpec, float | int, dict[str, str]]


@dataclass
class _Store:
    participant: Participant | None

    def get_participant(self, participant_id: str) -> Participant | None:
        if self.participant is None or participant_id != self.participant.id:
            return None
        return self.participant


@dataclass
class _Bridge:
    active: bool = True
    registered: list[tuple[MetricSpec, ...]] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)

    def register_specs(self, specs: tuple[MetricSpec, ...]) -> None:
        self.registered.append(specs)

    def observe(self, spec: MetricSpec, value: float | int, attributes: dict[str, str]) -> None:
        self.observations.append((spec, value, attributes))


def _participant() -> Participant:
    return Participant(id="agent", harness="codex", transcript_location="one.jsonl")


def _fact(
    native_id: str,
    *,
    kind: TrajectoryKind = TrajectoryKind.ASSISTANT,
    lane: TrajectoryLane = TrajectoryLane.MODEL,
    status: TrajectoryStatus = TrajectoryStatus.COMPLETED,
    timing: Timing | None = None,
    usage: TrajectoryUsage | None = None,
    details: tuple[DetailField, ...] = (),
    request_id: str | None = None,
    call_id: str | None = None,
    summary: str = "",
    raw_index: int = 1,
) -> TrajectoryFact:
    return TrajectoryFact(
        kind=kind,
        lane=lane,
        native_id=native_id,
        status=status,
        timing=timing,
        usage=usage,
        details=details,
        request_id=request_id,
        call_id=call_id,
        summary=summary,
        raw_index=raw_index,
    )


def _observations(bridge: _Bridge, name: str) -> list[Observation]:
    return [observation for observation in bridge.observations if observation[0].name == name]


def test_specs_have_exact_unique_schemas() -> None:
    assert len({spec.name for spec in AGENT_METRIC_SPECS}) == len(AGENT_METRIC_SPECS)
    schemas = [
        (spec.name, spec.kind, spec.unit, spec.attribute_keys) for spec in AGENT_METRIC_SPECS
    ]
    assert schemas == [
        (
            AGENT_REQUEST_DURATION_METRIC,
            MetricKind.HISTOGRAM,
            "ms",
            ("harness", "model", "result", "timing_provenance"),
        ),
        (
            AGENT_REQUEST_TTFT_METRIC,
            MetricKind.HISTOGRAM,
            "ms",
            ("harness", "model", "result", "timing_provenance"),
        ),
        (AGENT_TOKENS_METRIC, MetricKind.COUNTER, "{token}", ("harness", "model", "kind")),
        (AGENT_COST_METRIC, MetricKind.COUNTER, "USD", ("harness", "model", "provenance")),
        (
            AGENT_TOOL_DURATION_METRIC,
            MetricKind.HISTOGRAM,
            "ms",
            ("harness", "tool", "result", "timing_provenance"),
        ),
        (AGENT_FAILURES_METRIC, MetricKind.COUNTER, "{failure}", ("harness", "category")),
    ]
    assert AGENT_TELEMETRY_PARTICIPANT_STATE_LIMIT == 128
    assert AGENT_TELEMETRY_EMITTED_SIGNAL_LIMIT == 256
    assert AGENT_TELEMETRY_PARTICIPANT_STATE_LIMIT * AGENT_TELEMETRY_EMITTED_SIGNAL_LIMIT == 32_768


def test_create_requires_enabled_active_bridge_and_registers() -> None:
    store = _Store(_participant())
    inactive = _Bridge(active=False)
    active = _Bridge()

    assert create_agent_telemetry(store, active, enabled=False) is None
    assert create_agent_telemetry(store, None, enabled=True) is None
    assert create_agent_telemetry(store, inactive, enabled=True) is None
    assert create_agent_telemetry(store, active, enabled=True) is not None
    assert active.registered == [AGENT_METRIC_SPECS]


def test_usage_comes_only_from_new_events_with_estimated_cost_and_no_identity_attribute() -> None:
    bridge = _Bridge()
    telemetry_projector = AgentTelemetry(_Store(_participant()), bridge)  # type: ignore[arg-type]
    usage = TokenUsage(model="gpt-4o", input_tokens=3, output_tokens=5)
    event = Event(EventKind.ASSISTANT, usage=usage)
    batch = Batch(events=(event,))

    telemetry_projector.record_batch("agent", batch, ())
    assert bridge.observations == []

    telemetry_projector.record_batch("agent", batch, (event,))

    token_observations = _observations(bridge, AGENT_TOKENS_METRIC)
    assert [(value, attrs["kind"]) for _, value, attrs in token_observations] == [
        (3, "input"),
        (5, "output"),
    ]
    costs = _observations(bridge, AGENT_COST_METRIC)
    assert len(costs) == 1
    assert costs[0][1] > 0
    assert costs[0][2]["provenance"] == "estimated"
    assert all("participant_id" not in attributes for _, _, attributes in bridge.observations)


def test_terminal_direct_request_ttft_and_tool_duration_are_projected() -> None:
    bridge = _Bridge()
    projector = AgentTelemetry(_Store(_participant()), bridge)  # type: ignore[arg-type]
    request = _fact(
        "request",
        timing=Timing(
            start=1.0,
            first_token=1.2,
            duration_ms=500.0,
            provenance=TimingProvenance.DERIVED,
        ),
        usage=TrajectoryUsage(model="gpt-4o"),
    )
    tool = _fact(
        "tool",
        kind=TrajectoryKind.TOOL_RESULT,
        lane=TrajectoryLane.TOOLS,
        timing=Timing(duration_ms=25.0, provenance=TimingProvenance.SOURCE),
        details=(DetailField.from_text("tool", "exec"),),
        raw_index=2,
    )

    projector.record_batch("agent", Batch(trajectory=(request, tool)), ())

    duration = _observations(bridge, AGENT_REQUEST_DURATION_METRIC)
    ttft = _observations(bridge, AGENT_REQUEST_TTFT_METRIC)
    tools = _observations(bridge, AGENT_TOOL_DURATION_METRIC)
    assert duration[0][1] == 500.0
    assert ttft[0][1] == pytest.approx(200.0)
    assert duration[0][2] == {
        "harness": "codex",
        "model": "gpt-4o",
        "result": "success",
        "timing_provenance": "derived",
    }
    assert tools[0][1] == 25.0
    assert tools[0][2]["tool"] == "exec"


def test_batch_request_and_matched_tool_each_emit_once() -> None:
    bridge = _Bridge()
    projector = AgentTelemetry(_Store(_participant()), bridge)  # type: ignore[arg-type]
    request_first = _fact(
        "request-first",
        request_id="request-1",
        timing=Timing(duration_ms=10.0, provenance=TimingProvenance.SOURCE),
        raw_index=1,
    )
    request_last = _fact(
        "request-last",
        kind=TrajectoryKind.REASONING,
        request_id="request-1",
        timing=Timing(duration_ms=20.0, provenance=TimingProvenance.SOURCE),
        raw_index=2,
    )
    tool_call = _fact(
        "call",
        kind=TrajectoryKind.TOOL_CALL,
        lane=TrajectoryLane.TOOLS,
        status=TrajectoryStatus.RUNNING,
        call_id="call-1",
        timing=Timing(start=3.0, provenance=TimingProvenance.SOURCE),
        details=(DetailField.from_text("tool", "exec"),),
        raw_index=3,
    )
    tool_result = _fact(
        "result",
        kind=TrajectoryKind.TOOL_RESULT,
        lane=TrajectoryLane.TOOLS,
        call_id="call-1",
        timing=Timing(end=3.02, provenance=TimingProvenance.SOURCE),
        summary="tool result summary must not become the label",
        raw_index=4,
    )

    projector.record_batch(
        "agent",
        Batch(trajectory=(request_first, request_last, tool_call, tool_result)),
        (),
    )

    request_durations = _observations(bridge, AGENT_REQUEST_DURATION_METRIC)
    tool_durations = _observations(bridge, AGENT_TOOL_DURATION_METRIC)
    assert len(request_durations) == 1
    assert request_durations[0][1] == 20.0
    assert len(tool_durations) == 1
    assert tool_durations[0][1] == pytest.approx(20.0)
    assert tool_durations[0][2]["tool"] == "exec"


def test_running_or_missing_direct_timing_is_skipped_but_untimed_failure_counts() -> None:
    bridge = _Bridge()
    projector = AgentTelemetry(_Store(_participant()), bridge)  # type: ignore[arg-type]
    running = _fact("running", status=TrajectoryStatus.RUNNING, timing=Timing(duration_ms=4.0))
    untimed = _fact("untimed")
    failed = _fact("failed", status=TrajectoryStatus.ERROR, timing=None, raw_index=3)

    projector.record_batch("agent", Batch(trajectory=(running, untimed, failed)), ())

    assert _observations(bridge, AGENT_REQUEST_DURATION_METRIC) == []
    failures = _observations(bridge, AGENT_FAILURES_METRIC)
    assert len(failures) == 1
    assert failures[0][2]["category"] == AGENT_TELEMETRY_UNKNOWN_LABEL


def test_terminal_record_is_deduplicated_until_epoch_rotation_or_discard() -> None:
    bridge = _Bridge()
    projector = AgentTelemetry(_Store(_participant()), bridge)  # type: ignore[arg-type]
    fact = _fact("stable", timing=Timing(duration_ms=4.0, provenance=TimingProvenance.SOURCE))
    first = Batch(trajectory=(fact,))
    second = Batch(trajectory=(fact,), attached=Attachment("two.jsonl"))

    projector.record_batch("agent", first, ())
    projector.record_batch("agent", first, ())
    projector.record_batch("agent", second, ())
    projector.discard("agent")
    projector.record_batch("agent", second, ())

    assert len(_observations(bridge, AGENT_REQUEST_DURATION_METRIC)) == 3


def test_model_and_tool_caps_use_other_and_tool_never_uses_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telemetry, "AGENT_TELEMETRY_MODEL_CARDINALITY_LIMIT", 1)
    monkeypatch.setattr(telemetry, "AGENT_TELEMETRY_TOOL_CARDINALITY_LIMIT", 1)
    bridge = _Bridge()
    projector = AgentTelemetry(_Store(_participant()), bridge)  # type: ignore[arg-type]
    first_usage = Event(EventKind.ASSISTANT, usage=TokenUsage(model="first", input_tokens=1))
    second_usage = Event(EventKind.ASSISTANT, usage=TokenUsage(model="second", input_tokens=1))
    first_tool = _fact(
        "tool-one",
        kind=TrajectoryKind.TOOL_RESULT,
        lane=TrajectoryLane.TOOLS,
        timing=Timing(duration_ms=1.0, provenance=TimingProvenance.SOURCE),
        details=(DetailField.from_text("tool", "first-tool"),),
    )
    second_tool = _fact(
        "tool-two",
        kind=TrajectoryKind.TOOL_RESULT,
        lane=TrajectoryLane.TOOLS,
        timing=Timing(duration_ms=1.0, provenance=TimingProvenance.SOURCE),
        details=(DetailField.from_text("tool", "second-tool"),),
        raw_index=2,
    )
    no_identity = _fact(
        "tool-three",
        kind=TrajectoryKind.TOOL_RESULT,
        lane=TrajectoryLane.TOOLS,
        timing=Timing(duration_ms=1.0, provenance=TimingProvenance.SOURCE),
        summary="summary is never a tool label",
        raw_index=3,
    )

    projector.record_batch("agent", Batch(), (first_usage, second_usage))
    projector.record_batch("agent", Batch(trajectory=(first_tool, second_tool, no_identity)), ())

    token_models = [attrs["model"] for _, _, attrs in _observations(bridge, AGENT_TOKENS_METRIC)]
    tool_observations = _observations(bridge, AGENT_TOOL_DURATION_METRIC)
    tool_labels = [attrs["tool"] for _, _, attrs in tool_observations]
    assert token_models == ["first", AGENT_TELEMETRY_OTHER_LABEL]
    assert tool_labels == ["first-tool", AGENT_TELEMETRY_OTHER_LABEL, AGENT_TELEMETRY_UNKNOWN_LABEL]


def test_rich_fact_replaces_baseline_and_emits_one_signal() -> None:
    bridge = _Bridge()
    projector = AgentTelemetry(_Store(_participant()), bridge)  # type: ignore[arg-type]
    batch = Batch(
        events=(Event(EventKind.ASSISTANT, raw_index=7),),
        trajectory=(
            _fact(
                "rich",
                raw_index=7,
                timing=Timing(duration_ms=12.0, provenance=TimingProvenance.SOURCE),
            ),
        ),
    )

    projector.record_batch("agent", batch, ())

    durations = _observations(bridge, AGENT_REQUEST_DURATION_METRIC)
    assert len(durations) == 1
    assert durations[0][1] == 12.0
