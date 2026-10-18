"""The canonical trajectory record: wire form, usage and cost, identity and panel state."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from tests.support.trajectory_records import make_record
from theater.constants.trajectory import (
    TRAJECTORY_IDENTIFIER_MAX_BYTES,
)
from theater.daemon.trajectory.project import event_to_fact, event_to_record, fact_to_record
from theater.harness.contracts.events import Event, EventKind, EventPath, TokenUsage
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.trajectory import (
    ContentFormat,
    DetailField,
    PanelState,
    PanelStateInfo,
    Timing,
    TimingProvenance,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryPage,
    TrajectoryParticipantState,
    TrajectoryRecord,
    TrajectoryStatus,
    TrajectoryValidationError,
)
from theater.trajectory.enums import CostProvenance, TrajectoryFailureCategory
from theater.trajectory.identity import fallback_record_id
from theater.trajectory.records import TrajectoryFailure, TrajectoryUsage


def test_canonical_record_is_immutable_and_strictly_round_trips() -> None:
    record = make_record("epoch:1:0", details=(DetailField.from_text("output", "done"),))
    assert TrajectoryRecord.from_wire(record.to_wire()) == record
    with pytest.raises(FrozenInstanceError):
        record.summary = "changed"  # type: ignore[misc]

    invalid = record.to_wire()
    invalid["plugin_payload"] = {"not": "a wire field"}
    with pytest.raises(TrajectoryValidationError):
        TrajectoryRecord.from_wire(invalid)

    invalid_detail = record.to_wire()
    invalid_detail["details"] = [{"name": "output", "value": {"arbitrary": True}}]
    with pytest.raises(TrajectoryValidationError):
        TrajectoryRecord.from_wire(invalid_detail)


def test_additive_timing_usage_failure_and_retry_facts_keep_old_wire_compatible() -> None:
    old_timing = Timing.from_wire({"start": 1.0, "end": 2.0, "provenance": "source"})
    old_usage = TrajectoryUsage.from_wire(
        {
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
    )
    record = make_record("retry", timing=Timing(1.0, 2.0, first_token=1.5))

    assert old_timing.first_token is None
    assert old_usage.cost_provenance is CostProvenance.UNKNOWN
    assert record.timing is not None and record.timing.first_token == 1.5
    assert Timing(1.0, 2.0, 1_000.0, TimingProvenance.SOURCE).first_token is None
    assert "first_token" not in old_timing.to_wire()
    assert "cost_provenance" not in old_usage.to_wire()
    assert "failure" not in record.to_wire()
    assert "retry_of_record_id" not in record.to_wire()

    with pytest.raises(TrajectoryValidationError):
        Timing(start=2.0, first_token=1.0)
    with pytest.raises(TrajectoryValidationError):
        Timing(first_token=3.0, end=2.0)
    with pytest.raises(TrajectoryValidationError):
        Timing(first_token=float("nan"))
    with pytest.raises(TrajectoryValidationError):
        TrajectoryUsage(cost_provenance=CostProvenance.REPORTED)
    with pytest.raises(TrajectoryValidationError):
        TrajectoryFact(kind=TrajectoryKind.ERROR, retry_attempt=1)
    with pytest.raises(TrajectoryValidationError):
        TrajectoryFact(kind=TrajectoryKind.ERROR, retry_of_native_id="first", retry_attempt=0)

    linked = TrajectoryFact(kind=TrajectoryKind.ERROR, retry_of_native_id="first")
    assert linked.retry_of_native_id == "first" and linked.retry_attempt is None
    assert make_record("retry-record", retry_of_record_id="first").retry_attempt is None

    fact = TrajectoryFact(
        kind=TrajectoryKind.ERROR,
        failure=TrajectoryFailure(TrajectoryFailureCategory.PROVIDER, code="rate_limit"),
        retry_of_native_id="first",
        retry_attempt=2,
    )
    projected = fact_to_record(fact, participant_id="p", source_epoch="epoch")
    assert projected.failure == fact.failure
    assert projected.retry_of_record_id == "epoch:first"
    assert projected.retry_attempt == 2
    assert TrajectoryRecord.from_wire(projected.to_wire()) == projected


def test_timing_diagnostics_and_usage_provider_are_explicit() -> None:
    timing = Timing(start=10.0, first_token=10.25, end=11.25)
    usage = TrajectoryUsage(
        model="model",
        provider="provider",
        output_tokens=100,
        cost_usd=0.2,
        cost_provenance=CostProvenance.REPORTED,
    )

    assert timing.ttft_ms == 250
    assert timing.generation_duration_ms == 1000
    assert TrajectoryUsage.from_wire(usage.to_wire()) == usage


def test_baseline_usage_preserves_cost_provenance() -> None:
    fact = event_to_fact(
        Event(
            kind=EventKind.ASSISTANT,
            usage=TokenUsage(cost_usd=0.1, cost_provenance=CostProvenance.REPORTED),
        )
    )

    assert fact.usage is not None
    assert fact.usage.cost_provenance is CostProvenance.REPORTED


def test_baseline_projection_preserves_structured_event_paths() -> None:
    record = event_to_record(
        Event(
            kind=EventKind.TOOL_CALL,
            tool_name="read_file",
            paths=(EventPath("src/app.py", "read"),),
        ),
        participant_id="p",
        source_epoch="epoch",
    )

    path = next(detail for detail in record.details if detail.format is ContentFormat.PATH)
    assert path.name == "path.read"
    assert path.preview.text == "src/app.py"


def test_canonical_projection_estimates_missing_usage_cost() -> None:
    fact = TrajectoryFact(
        kind=TrajectoryKind.ASSISTANT,
        usage=TrajectoryUsage(
            model="claude-sonnet-5",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=1_000_000,
            cache_write_tokens=1_000_000,
        ),
    )

    record = fact_to_record(fact, participant_id="p", source_epoch="epoch")

    assert record.usage is not None
    assert record.usage.cost_usd == pytest.approx(14.7)
    assert record.usage.cost_provenance is CostProvenance.ESTIMATED


def test_canonical_projection_keeps_reported_usage_cost() -> None:
    fact = TrajectoryFact(
        kind=TrajectoryKind.ASSISTANT,
        usage=TrajectoryUsage(
            model="claude-sonnet-5",
            input_tokens=1_000_000,
            cost_usd=0.25,
            cost_provenance=CostProvenance.REPORTED,
        ),
    )

    record = fact_to_record(fact, participant_id="p", source_epoch="epoch")

    assert record.usage is not None
    assert record.usage.cost_usd == 0.25
    assert record.usage.cost_provenance is CostProvenance.REPORTED


def test_canonical_projection_does_not_price_model_metadata_as_zero_usage() -> None:
    fact = TrajectoryFact(
        kind=TrajectoryKind.ASSISTANT,
        usage=TrajectoryUsage(model="claude-sonnet-5"),
    )

    record = fact_to_record(fact, participant_id="p", source_epoch="epoch")

    assert record.usage is not None
    assert record.usage.cost_usd is None
    assert record.usage.cost_provenance is CostProvenance.UNKNOWN


def test_reasoning_wire_and_status_projection_are_explicit() -> None:
    reasoning = TrajectoryRecord(
        record_id="reasoning",
        revision=0,
        participant_id="participant",
        source_epoch="source",
        lane=TrajectoryLane.MODEL,
        kind=TrajectoryKind.REASONING,
        source="harness",
        summary="visible reasoning summary",
        status=TrajectoryStatus.COMPLETED,
    )
    assert TrajectoryRecord.from_wire(reasoning.to_wire()).kind is TrajectoryKind.REASONING
    assert (
        event_to_fact(Event(kind=EventKind.USER, text="prompt")).status
        is TrajectoryStatus.COMPLETED
    )
    assert (
        event_to_fact(Event(kind=EventKind.ASSISTANT, text="old")).status
        is TrajectoryStatus.COMPLETED
    )
    assert (
        event_to_fact(Event(kind=EventKind.ASSISTANT, text="live")).status
        is TrajectoryStatus.COMPLETED
    )


def test_source_local_native_ids_are_always_namespaced() -> None:
    native = TrajectoryFact(kind=TrajectoryKind.ASSISTANT, native_id="message-1")
    bus = TrajectoryFact(kind=TrajectoryKind.THEATER, native_id="bus:7")
    native_record = fact_to_record(native, participant_id="p", source_epoch="epoch")
    bus_record = fact_to_record(bus, participant_id="p", source_epoch="epoch")
    assert native_record.record_id == "epoch:message-1"
    assert native_record.native_id == "epoch:message-1"
    assert bus_record.record_id == "epoch:bus:7"
    assert bus_record.native_id == "epoch:bus:7"
    assert (
        bus_record.record_id
        != fact_to_record(bus, participant_id="p", source_epoch="other").record_id
    )


def test_fallback_identity_is_bounded_and_deterministic() -> None:
    value = fallback_record_id("epoch" * 200, 10**100, 10**100)
    assert len(value.encode("utf-8")) <= TRAJECTORY_IDENTIFIER_MAX_BYTES
    assert value == fallback_record_id("epoch" * 200, 10**100, 10**100)


def test_identifier_controls_are_rejected_without_display_sanitization() -> None:
    literal = TrajectoryFact(kind=TrajectoryKind.ASSISTANT, native_id=r"\x00")
    assert literal.native_id == r"\x00"
    with pytest.raises(TrajectoryValidationError):
        TrajectoryFact(kind=TrajectoryKind.ASSISTANT, native_id="\x00")
    with pytest.raises(TrajectoryValidationError):
        DetailField.from_text("field\x00", "value")


def test_participant_state_is_separate_from_panel_state() -> None:
    info = PanelStateInfo(
        PanelState.UNAVAILABLE,
        "no trajectory source",
        TrajectoryParticipantState.DEAD,
    )
    assert PanelStateInfo.from_wire(info.to_wire()) == info


def test_panel_state_wire_round_trip() -> None:
    page = TrajectoryPage(panel_state=PanelStateInfo(PanelState.WAITING, "waiting for transcript"))
    assert TrajectoryPage.from_wire(page.to_wire()) == page
