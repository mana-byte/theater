"""Focused tests for the trajectory domain and additive harness seams."""

from __future__ import annotations

import re
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from theater.constants.trajectory import (
    TRAJECTORY_DETAIL_NAME_MAX_BYTES,
    TRAJECTORY_DETAIL_RECORD_MAX_BYTES,
    TRAJECTORY_IDENTIFIER_MAX_BYTES,
    TRAJECTORY_MAX_COVERAGE_GAPS,
    TRAJECTORY_MAX_DETAILS_PER_RECORD,
    TRAJECTORY_MAX_GROUP_CHILDREN,
    TRAJECTORY_MAX_GROUP_RECORD_IDS,
    TRAJECTORY_TRANSCRIPT_HISTORY_MAX_SCAN_BYTES,
    TRAJECTORY_TRANSCRIPT_HISTORY_WINDOW_BYTES,
)
from theater.daemon.trajectory.project import event_to_fact, event_to_record, fact_to_record
from theater.harness.contracts.events import Event, EventKind, EventPath, TokenUsage
from theater.harness.contracts.source import (
    Batch,
    History,
    HistoryPage,
    Source,
    SourceContractError,
)
from theater.harness.contracts.trajectory import ParsedRecord, TrajectoryFact
from theater.harness.transcript.observer import TranscriptObserver, open_participant_source
from theater.harness.transcript.source import TranscriptSource
from theater.trajectory import (
    ContentFormat,
    ContentPreview,
    CoverageGap,
    DetailField,
    GroupKind,
    PanelState,
    PanelStateInfo,
    Timing,
    TimingProvenance,
    TrajectoryCoverage,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryPage,
    TrajectoryParticipantState,
    TrajectoryRecord,
    TrajectoryStatus,
    TrajectoryValidationError,
    bounded_preview,
    deterministic_record_order,
    group_records,
    merge_records,
)
from theater.trajectory.enums import CostProvenance, TrajectoryFailureCategory
from theater.trajectory.identity import fallback_record_id
from theater.trajectory.records import TrajectoryFailure, TrajectoryUsage


def make_record(
    record_id: str,
    *,
    revision: int = 0,
    raw_index: int = 0,
    source_epoch: str = "epoch",
    turn_id: str | None = None,
    step_id: str | None = None,
    details: tuple[DetailField, ...] = (),
    timing: Timing | None = None,
    call_id: str | None = None,
    parent_call_id: str | None = None,
    retry_of_record_id: str | None = None,
    retry_attempt: int | None = None,
) -> TrajectoryRecord:
    return TrajectoryRecord(
        record_id=record_id,
        revision=revision,
        participant_id="participant",
        source_epoch=source_epoch,
        lane=TrajectoryLane.MODEL,
        kind=TrajectoryKind.ASSISTANT,
        source="baseline",
        summary="answer",
        status=TrajectoryStatus.COMPLETED,
        raw_index=raw_index,
        turn_id=turn_id,
        step_id=step_id,
        timing=timing,
        call_id=call_id,
        parent_call_id=parent_call_id,
        retry_of_record_id=retry_of_record_id,
        retry_attempt=retry_attempt,
        details=details,
    )


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


def test_utf8_preview_keeps_safe_head_tail_and_exact_omission() -> None:
    original = "é" * 20_000
    preview = bounded_preview(original)
    marker = f"… {preview.omitted_bytes} bytes omitted …"
    shown = preview.text.replace(marker, "", 1)
    assert len(preview.text.encode("utf-8")) <= 16 * 1024
    assert preview.omitted_bytes == len(original.encode("utf-8")) - len(shown.encode("utf-8"))
    assert shown.startswith("é") and shown.endswith("é")
    assert marker in preview.text

    unsafe = bounded_preview("\x1b[31m[bold]\x00")
    assert "\x1b" not in unsafe.text
    assert "\x00" not in unsafe.text
    assert unsafe.text == r"\x1b[31m[bold]\x00"
    assert ContentPreview(r"[bold]\literal").text == r"[bold]\literal"


def test_record_detail_fields_obey_field_and_aggregate_byte_caps() -> None:
    details = tuple(
        DetailField.from_text(str(index), "x" * (20 * 1024), format=ContentFormat.TEXT)
        for index in range(3)
    )
    record = make_record("r", details=details)
    assert all(detail.preview.encoded_bytes <= 16 * 1024 for detail in record.details)
    assert sum(detail.preview.encoded_bytes for detail in record.details) <= 32 * 1024
    assert any(detail.preview.omitted_bytes for detail in record.details)


def test_projection_identity_revision_and_grouping_are_deterministic() -> None:
    assert fallback_record_id("trusted-epoch", 4, 2) == "trusted-epoch:4:2"
    event = Event(
        kind=EventKind.ASSISTANT, text="done", raw_index=4, turn_id="turn-1", turn_end=True
    )
    record = event_to_record(event, participant_id="p", source_epoch="trusted-epoch")
    assert record.record_id == "trusted-epoch:4:0"

    old = make_record("native", revision=1)
    new = make_record("native", revision=2)
    assert merge_records((new,), (old,)) == (new,)

    groups = group_records(
        (
            make_record("unplaced", raw_index=0),
            make_record("step", raw_index=1, turn_id="t1", step_id="s1"),
            make_record("turn", raw_index=2, turn_id="t1"),
        )
    )
    assert groups[0].kind is GroupKind.BETWEEN_TURNS
    assert groups[1].kind is GroupKind.TURN
    assert groups[1].children[0].kind is GroupKind.STEP


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


def test_cross_stream_order_does_not_use_lexical_epoch_order() -> None:
    records = (
        make_record("z-2", source_epoch="z", raw_index=2),
        make_record("a-1", source_epoch="a", raw_index=1),
        make_record("z-1", source_epoch="z", raw_index=1),
    )
    ordered = deterministic_record_order(records)
    assert [record.record_id for record in ordered] == ["z-1", "z-2", "a-1"]
    groups = group_records(records)
    assert len(groups) == 1
    assert groups[0].kind is GroupKind.BETWEEN_TURNS


def test_mixed_stream_grouping_preserves_transcript_turns() -> None:
    transcript_step = make_record(
        "step",
        source_epoch="transcript",
        raw_index=1,
        turn_id="turn-1",
        step_id="step-1",
        call_id="call-1",
    )
    transcript_turn = make_record(
        "turn",
        source_epoch="transcript",
        raw_index=2,
        turn_id="turn-1",
    )
    theater = make_record("theater", source_epoch="theater", raw_index=1)
    groups = group_records((transcript_step, transcript_turn, theater))
    assert [group.kind for group in groups] == [GroupKind.TURN, GroupKind.BETWEEN_TURNS]
    assert groups[0].children[0].record_ids == ("step",)
    assert groups[1].record_ids == ("theater",)


def test_grouping_chunks_turns_larger_than_the_wire_group_bounds() -> None:
    direct = tuple(
        make_record(f"direct-{index}", raw_index=index, turn_id="large-turn")
        for index in range(TRAJECTORY_MAX_GROUP_RECORD_IDS * 2 + 1)
    )
    direct_groups = group_records(direct)

    assert [len(group.record_ids) for group in direct_groups] == [
        TRAJECTORY_MAX_GROUP_RECORD_IDS,
        TRAJECTORY_MAX_GROUP_RECORD_IDS,
        1,
    ]
    assert tuple(record_id for group in direct_groups for record_id in group.record_ids) == tuple(
        record.record_id for record in direct
    )
    assert len({group.group_id for group in direct_groups}) == len(direct_groups)

    stepped = tuple(
        make_record(
            f"step-{index}",
            raw_index=index,
            turn_id="large-turn",
            step_id="large-step",
        )
        for index in range(TRAJECTORY_MAX_GROUP_RECORD_IDS * 2 + 1)
    )
    step_groups = tuple(child for group in group_records(stepped) for child in group.children)

    assert [len(group.record_ids) for group in step_groups] == [
        TRAJECTORY_MAX_GROUP_RECORD_IDS,
        TRAJECTORY_MAX_GROUP_RECORD_IDS,
        1,
    ]
    assert tuple(record_id for group in step_groups for record_id in group.record_ids) == tuple(
        record.record_id for record in stepped
    )
    assert len({group.group_id for group in step_groups}) == len(step_groups)

    at_limit = direct[:TRAJECTORY_MAX_GROUP_RECORD_IDS]
    assert len(group_records(at_limit)) == 1


def test_grouping_chunks_turn_records_in_source_order() -> None:
    call = make_record(
        "call",
        raw_index=0,
        turn_id="large-turn",
        call_id="call-1",
    )
    linked = make_record("linked", raw_index=1, parent_call_id="call-1")
    bulk = tuple(
        make_record(f"direct-{index}", raw_index=index + 2, turn_id="large-turn")
        for index in range(TRAJECTORY_MAX_GROUP_RECORD_IDS)
    )
    records = (call, linked, *bulk)

    groups = group_records(records)

    assert tuple(record_id for group in groups for record_id in group.record_ids) == tuple(
        record.record_id for record in records
    )


def test_grouping_chunks_mixed_turn_units_at_each_bound() -> None:
    records = tuple(
        record
        for index in range(TRAJECTORY_MAX_GROUP_RECORD_IDS + 1)
        for record in (
            make_record(f"direct-{index}", raw_index=index * 2, turn_id="large-turn"),
            make_record(
                f"step-{index}",
                raw_index=index * 2 + 1,
                turn_id="large-turn",
                step_id=f"step-{index}",
            ),
        )
    )

    groups = group_records(records)

    assert [(len(group.record_ids), len(group.children)) for group in groups] == [
        (TRAJECTORY_MAX_GROUP_RECORD_IDS, TRAJECTORY_MAX_GROUP_CHILDREN),
        (1, 1),
    ]


def test_grouping_chunks_turns_with_too_many_step_groups() -> None:
    records = tuple(
        make_record(
            f"record-{index}",
            raw_index=index,
            turn_id="large-turn",
            step_id=f"step-{index}",
        )
        for index in range(TRAJECTORY_MAX_GROUP_CHILDREN + 1)
    )

    groups = group_records(records)

    assert [len(group.children) for group in groups] == [TRAJECTORY_MAX_GROUP_CHILDREN, 1]
    assert len({group.group_id for group in groups}) == len(groups)


def test_grouping_chunks_large_between_turn_buckets() -> None:
    records = tuple(
        make_record(f"between-{index}", raw_index=index)
        for index in range(TRAJECTORY_MAX_GROUP_RECORD_IDS * 2 + 1)
    )

    groups = group_records(records)

    assert [len(group.record_ids) for group in groups] == [
        TRAJECTORY_MAX_GROUP_RECORD_IDS,
        TRAJECTORY_MAX_GROUP_RECORD_IDS,
        1,
    ]
    assert tuple(record_id for group in groups for record_id in group.record_ids) == tuple(
        record.record_id for record in records
    )
    assert len({group.group_id for group in groups}) == len(groups)
    assert len(group_records(records[:TRAJECTORY_MAX_GROUP_RECORD_IDS])) == 1


def test_exact_call_link_positions_cross_stream_record() -> None:
    transcript = make_record(
        "call",
        source_epoch="transcript",
        raw_index=1,
        turn_id="turn-1",
        step_id="step-1",
        call_id="call-1",
    )
    theater = make_record(
        "result",
        source_epoch="theater",
        raw_index=1,
        parent_call_id="call-1",
    )
    groups = group_records((transcript, theater))
    assert len(groups) == 1
    assert groups[0].children[0].record_ids == ("call", "result")


def test_reliable_timestamps_place_theater_only_between_turns() -> None:
    first = make_record(
        "first",
        source_epoch="transcript",
        raw_index=1,
        turn_id="turn-1",
        timing=Timing(0, 1, provenance=TimingProvenance.SOURCE),
    )
    second = make_record(
        "second",
        source_epoch="transcript",
        raw_index=2,
        turn_id="turn-2",
        timing=Timing(3, 4, provenance=TimingProvenance.SOURCE),
    )
    theater = make_record(
        "between",
        source_epoch="theater",
        raw_index=1,
        timing=Timing(2, 2.5, provenance=TimingProvenance.SOURCE),
    )
    groups = group_records((first, second, theater))
    assert [group.kind for group in groups] == [
        GroupKind.TURN,
        GroupKind.BETWEEN_TURNS,
        GroupKind.TURN,
    ]
    assert groups[1].record_ids == ("between",)


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


def test_unpositioned_bus_records_are_ordered_by_bus_id() -> None:
    groups = group_records(
        (
            make_record("bus:10", source_epoch="theater", raw_index=1),
            make_record("bus:2", source_epoch="theater", raw_index=2),
        )
    )
    assert groups[0].record_ids == ("bus:2", "bus:10")


def test_between_group_ids_survive_prepend_and_regrouping() -> None:
    first = make_record(
        "first",
        source_epoch="transcript",
        raw_index=1,
        turn_id="turn-1",
        timing=Timing(0, 1, provenance=TimingProvenance.SOURCE),
    )
    second = make_record(
        "second",
        source_epoch="transcript",
        raw_index=2,
        turn_id="turn-2",
        timing=Timing(3, 4, provenance=TimingProvenance.SOURCE),
    )
    between = make_record(
        "between",
        source_epoch="theater",
        raw_index=1,
        timing=Timing(2, 2.5, provenance=TimingProvenance.SOURCE),
    )
    initial = group_records((first, second, between))
    prepended = group_records(
        (make_record("bus:1", source_epoch="theater"), first, second, between)
    )
    initial_id = next(group.group_id for group in initial if group.record_ids == ("between",))
    prepended_id = next(group.group_id for group in prepended if group.record_ids == ("between",))
    assert initial_id == prepended_id
    assert any(group.group_id == "between-turns:unpositioned" for group in prepended)


def test_detail_rebounding_keeps_one_accurate_omission_marker() -> None:
    marker_pattern = re.compile(r"… (\d+) bytes omitted …")
    record = make_record(
        "bounded",
        details=(
            DetailField.from_text("first", "\x00" * 12_000 + "a" * 10_000),
            DetailField.from_text("second", "\x1b" * 12_000 + "b" * 10_000),
            DetailField.from_text("third", "c" * 20_000),
        ),
    )
    assert len(record.details) == 2
    for detail in record.details:
        matches = marker_pattern.findall(detail.preview.text)
        assert len(matches) == 1
        assert int(matches[0]) == detail.preview.omitted_bytes


def test_history_page_rejects_silent_output_truncation() -> None:
    events = tuple(Event(kind=EventKind.ASSISTANT, text=str(index)) for index in range(201))
    with pytest.raises(SourceContractError):
        HistoryPage(events=events)


def test_adversarial_bounds_cover_names_details_groups_and_gaps() -> None:
    with pytest.raises(TrajectoryValidationError):
        DetailField.from_text("x" * (TRAJECTORY_DETAIL_NAME_MAX_BYTES + 1), "value")
    with pytest.raises(TrajectoryValidationError):
        make_record("x" * 513)
    details = tuple(
        DetailField.from_text(str(index), "value")
        for index in range(TRAJECTORY_MAX_DETAILS_PER_RECORD + 10)
    )
    record = make_record("bounded", details=details)
    assert len(record.details) <= TRAJECTORY_MAX_DETAILS_PER_RECORD
    assert (
        sum(
            len(detail.name.encode("utf-8")) + detail.preview.encoded_bytes
            for detail in record.details
        )
        <= TRAJECTORY_DETAIL_RECORD_MAX_BYTES
    )
    assert all(detail.preview.text for detail in record.details)
    with pytest.raises(TrajectoryValidationError):
        TrajectoryCoverage(
            gaps=tuple(
                CoverageGap("stream", "gap") for _ in range(TRAJECTORY_MAX_COVERAGE_GAPS + 1)
            )
        )


def test_participant_state_is_separate_from_panel_state() -> None:
    info = PanelStateInfo(
        PanelState.UNAVAILABLE,
        "no trajectory source",
        TrajectoryParticipantState.DEAD,
    )
    assert PanelStateInfo.from_wire(info.to_wire()) == info


def test_raw_text_preserves_literal_markup_and_is_bounded() -> None:
    raw = "[bold]\\literal " + "x" * (20 * 1024)
    fact = event_to_fact(
        Event(kind=EventKind.ASSISTANT, text="clipped", raw_text=raw),
    )
    assert fact.details[0].name == "raw"
    assert fact.details[0].preview.text.startswith("[bold]\\literal")
    assert fact.details[0].preview.encoded_bytes <= 16 * 1024
    record = fact_to_record(fact, participant_id="p", source_epoch="epoch")
    assert record.details[0].preview.text == fact.details[0].preview.text
    assert len(record.summary.encode("utf-8")) <= 16 * 1024


def test_control_escaping_does_not_inflate_omitted_source_bytes() -> None:
    original = "\x1b" + "x" * 20_000
    preview = bounded_preview(original)
    marker = f"… {preview.omitted_bytes} bytes omitted …"
    assert marker in preview.text
    displayed_without_marker = preview.text.replace(marker, "", 1)
    assert preview.omitted_bytes == len(original.encode("utf-8")) - (
        len(displayed_without_marker.encode("utf-8")) - 3
    )


class LegacyCountingObserver(TranscriptObserver):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.parse_calls = 0

    def find_transcript(
        self, *, cwd: str, session_id: str | None = None, after: float | None = None
    ):
        return self.path

    def session_id(self, transcript: Path) -> str | None:
        return "session"

    def parse(self, line: str, index: int, *, clip_text: bool = True) -> list[Event]:
        self.parse_calls += 1
        return [Event(kind=EventKind.ASSISTANT, text=line, raw_index=index)]

    def is_idle_screen(self, capture: str) -> bool:
        return False


class RichObserver(LegacyCountingObserver):
    def parse_record(self, line: str, index: int, *, clip_text: bool = True) -> ParsedRecord:
        self.parse_calls += 1
        return ParsedRecord(
            events=(Event(kind=EventKind.ASSISTANT, text=line, raw_index=index),),
            trajectory=(
                TrajectoryFact(
                    kind=TrajectoryKind.ASSISTANT,
                    source="rich-test",
                    native_id=f"native-{index}",
                    raw_index=index,
                ),
            ),
        )


class OrderedObserver(LegacyCountingObserver):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.seen: list[str] = []

    def parse_record(self, line: str, index: int, *, clip_text: bool = True) -> ParsedRecord:
        self.seen.append(line)
        return ParsedRecord(
            events=(Event(kind=EventKind.ASSISTANT, text=line, raw_index=index),),
        )


class MultiRecordObserver(LegacyCountingObserver):
    def __init__(self, path: Path, output_count: int = 2) -> None:
        super().__init__(path)
        self.output_count = output_count

    def parse_record(self, line: str, index: int, *, clip_text: bool = True) -> ParsedRecord:
        self.parse_calls += 1
        return ParsedRecord(
            events=tuple(
                Event(kind=EventKind.ASSISTANT, text=f"{line}-event-{ordinal}", raw_index=index)
                for ordinal in range(self.output_count)
            ),
            trajectory=tuple(
                TrajectoryFact(
                    kind=TrajectoryKind.ASSISTANT,
                    source="multi-test",
                    native_id=f"{line}-fact-{ordinal}",
                    raw_index=index,
                    event_ordinal=ordinal,
                )
                for ordinal in range(self.output_count)
            ),
        )


class SuppressingObserver(LegacyCountingObserver):
    def parse_record(self, line: str, index: int, *, clip_text: bool = True) -> ParsedRecord:
        self.parse_calls += 1
        event = Event(kind=EventKind.ASSISTANT, text=line, raw_index=index)
        return ParsedRecord(events=(event,), trajectory_events=())


class RawObserver(LegacyCountingObserver):
    def parse(self, line: str, index: int, *, clip_text: bool = True) -> list[Event]:
        self.parse_calls += 1
        return [
            Event(
                kind=EventKind.ASSISTANT,
                text=line,
                raw_text="[raw]\\literal " + "x" * (20 * 1024),
                raw_index=index,
            )
        ]


class InvalidRecordObserver(LegacyCountingObserver):
    def parse_record(self, line: str, index: int, *, clip_text: bool = True):
        self.parse_calls += 1
        return [Event(kind=EventKind.ASSISTANT, text=line, raw_index=index)]


async def test_default_parse_record_calls_legacy_parse_once() -> None:
    path = Path("/tmp/unused-transcript")
    observer = LegacyCountingObserver(path)
    parsed = observer.parse_record("line", 3)
    assert len(parsed.events) == 1
    assert parsed.trajectory == ()
    assert observer.parse_calls == 1


async def test_invalid_parse_record_result_raises_without_legacy_fallback(tmp_path) -> None:
    path = tmp_path / "transcript.jsonl"
    path.write_text("one\n", encoding="utf-8")
    observer = InvalidRecordObserver(path)
    source = TranscriptSource(observer, cwd=str(tmp_path))
    with pytest.raises(SourceContractError):
        await source.read()
    assert observer.parse_calls == 1


async def test_history_page_parses_selected_records_in_transcript_order(tmp_path) -> None:
    path = tmp_path / "ordered.jsonl"
    path.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    observer = OrderedObserver(path)
    source = TranscriptSource(observer, cwd=str(tmp_path))

    page = await source.history_page(limit=3)

    assert observer.seen == ["two", "three", "four"]
    assert [event.text for event in page.events] == ["two", "three", "four"]


async def test_transcript_live_reads_emit_facts_and_history_pages_do_not_move_cursor(
    tmp_path,
) -> None:
    path = tmp_path / "transcript.jsonl"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    observer = RichObserver(path)
    source = TranscriptSource(observer, cwd=str(tmp_path))

    attached = await source.read()
    assert attached.attached is not None
    source.commit_attachment()
    before_page = (source.path, source.offset, source.index, source.mtime)

    page = await source.history_page(limit=1)
    assert [event.text for event in page.events] == ["three"]
    assert [fact.native_id for fact in page.trajectory] == ["native-2"]
    assert page.has_older is True
    assert page.older_cursor is not None
    assert (source.path, source.offset, source.index, source.mtime) == before_page

    older = await source.history_page(before=page.older_cursor, limit=1)
    assert [event.text for event in older.events] == ["two"]
    assert older.has_older is True
    assert (source.path, source.offset, source.index, source.mtime) == before_page

    with path.open("a", encoding="utf-8") as handle:
        handle.write("four\n")
    live = await source.read()
    assert [event.text for event in live.events] == ["four"]
    assert [fact.native_id for fact in live.trajectory] == ["native-3"]
    assert observer.parse_calls == 4


async def test_transcript_source_keeps_control_events_out_of_opted_out_trajectory(tmp_path) -> None:
    path = tmp_path / "duplicate.jsonl"
    path.write_text("one\n", encoding="utf-8")
    source = TranscriptSource(SuppressingObserver(path), cwd=str(tmp_path))
    attached = await source.read()
    assert attached.attached is not None
    source.commit_attachment()

    page = await source.history_page(limit=1)
    assert [event.text for event in page.events] == ["one"]
    assert page.trajectory_events == ()

    with path.open("a", encoding="utf-8") as handle:
        handle.write("two\n")
    batch = await source.read()
    assert [event.text for event in batch.events] == ["two"]
    assert batch.trajectory_events == ()


async def test_history_pages_keep_complete_multi_output_records(tmp_path) -> None:
    path = tmp_path / "multi.jsonl"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    observer = MultiRecordObserver(path)
    source = TranscriptSource(observer, cwd=str(tmp_path))
    attached = await source.read()
    assert attached.attached is not None
    source.commit_attachment()

    pages = []
    page = await source.history_page(limit=2)
    while True:
        pages.append(page)
        if page.older_cursor is None:
            break
        page = await source.history_page(before=page.older_cursor, limit=2)

    events = [event.text for page in reversed(pages) for event in page.events]
    facts = [fact.native_id for page in reversed(pages) for fact in page.trajectory]
    assert events == [
        "one-event-0",
        "one-event-1",
        "two-event-0",
        "two-event-1",
        "three-event-0",
        "three-event-1",
    ]
    assert facts == [
        "one-fact-0",
        "one-fact-1",
        "two-fact-0",
        "two-fact-1",
        "three-fact-0",
        "three-fact-1",
    ]


async def test_history_page_reports_a_raw_record_over_limit(tmp_path) -> None:
    path = tmp_path / "oversized-output.jsonl"
    path.write_text("one\n", encoding="utf-8")
    observer = MultiRecordObserver(path, output_count=3)
    source = TranscriptSource(observer, cwd=str(tmp_path))
    attached = await source.read()
    assert attached.attached is not None
    source.commit_attachment()

    page = await source.history_page(limit=2)
    assert page.error_code == "history_record_too_large"
    assert page.older_cursor is None
    assert page.has_older is False


async def test_history_cursor_invalidates_after_rewrite_and_rotation(tmp_path) -> None:
    path = tmp_path / "transcript.jsonl"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    observer = RichObserver(path)
    source = TranscriptSource(observer, cwd=str(tmp_path))
    attached = await source.read()
    assert attached.attached is not None
    source.commit_attachment()
    page = await source.history_page(limit=1)
    assert page.older_cursor is not None

    path.write_text("rewritten\ntwo\nthree\n", encoding="utf-8")
    rewritten = await source.history_page(before=page.older_cursor, limit=1)
    assert rewritten.error_code == "history_cursor_invalid"

    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    page = await source.history_page(limit=1)
    assert page.older_cursor is not None
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text("replacement\n", encoding="utf-8")
    replacement.replace(path)
    rotated = await source.history_page(before=page.older_cursor, limit=1)
    assert rotated.error_code == "history_cursor_invalid"


async def test_history_cursor_survives_append(tmp_path) -> None:
    path = tmp_path / "append.jsonl"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    observer = LegacyCountingObserver(path)
    source = TranscriptSource(observer, cwd=str(tmp_path))
    attached = await source.read()
    assert attached.attached is not None
    source.commit_attachment()
    page = await source.history_page(limit=1)
    assert page.older_cursor is not None

    with path.open("a", encoding="utf-8") as handle:
        handle.write("four\n")

    older = await source.history_page(before=page.older_cursor, limit=1)
    assert older.error_code is None
    assert [event.text for event in older.events] == ["two"]


async def test_history_page_work_is_bounded_to_reverse_window(tmp_path, monkeypatch) -> None:
    path = tmp_path / "large.jsonl"
    path.write_text("".join(f"record-{index}\n" for index in range(200_000)), encoding="utf-8")
    observer = LegacyCountingObserver(path)
    source = TranscriptSource(observer, cwd=str(tmp_path))
    attached = await source.read()
    assert attached.attached is not None
    source.commit_attachment()

    bytes_read = [0]
    real_open = Path.open

    class CountingFile:
        def __init__(self, handle) -> None:
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def read(self, *args):
            value = self.handle.read(*args)
            bytes_read[0] += len(value)
            return value

        def __getattr__(self, name):
            return getattr(self.handle, name)

    def counted_open(file_path, *args, **kwargs):
        return CountingFile(real_open(file_path, *args, **kwargs))

    monkeypatch.setattr(Path, "open", counted_open)
    page = await source.history_page(limit=1)
    assert [event.text for event in page.events] == ["record-199999"]
    assert bytes_read[0] < path.stat().st_size


@pytest.mark.parametrize(
    ("line_bytes", "error_code"),
    [
        (TRAJECTORY_TRANSCRIPT_HISTORY_WINDOW_BYTES + 32, None),
        (TRAJECTORY_TRANSCRIPT_HISTORY_MAX_SCAN_BYTES + 32, "history_record_too_large"),
    ],
)
async def test_history_page_bounds_oversized_records(tmp_path, line_bytes, error_code) -> None:
    path = tmp_path / "oversized.jsonl"
    path.write_bytes(b"x" * line_bytes + b"\nsmall\n")
    observer = LegacyCountingObserver(path)
    source = TranscriptSource(observer, cwd=str(tmp_path))
    attached = await source.read()
    assert attached.attached is not None
    source.commit_attachment()
    newest = await source.history_page(limit=1)
    assert newest.older_cursor is not None

    older = await source.history_page(before=newest.older_cursor, limit=1)
    assert older.error_code == error_code
    if error_code is None:
        assert older.events
        assert older.older_cursor is None
    else:
        assert older.events == ()
        assert older.older_cursor is None


async def test_history_page_does_not_return_a_self_cursor_for_a_partial_record(tmp_path) -> None:
    path = tmp_path / "partial.jsonl"
    path.write_bytes(b"partial record")
    observer = LegacyCountingObserver(path)
    source = TranscriptSource(observer, cwd=str(tmp_path))
    attached = await source.read()
    assert attached.attached is not None
    source.commit_attachment()

    page = await source.history_page(limit=1)
    assert page.events == ()
    assert page.has_older is False
    assert page.older_cursor is None


async def test_baseline_fallback_identity_uses_source_offsets_across_reads(tmp_path) -> None:
    path = tmp_path / "coordinates.jsonl"
    path.write_text("one\n", encoding="utf-8")
    observer = LegacyCountingObserver(path)
    watcher = TranscriptSource(observer, cwd=str(tmp_path))
    attached = await watcher.read()
    assert attached.attached is not None
    watcher.commit_attachment()
    with path.open("a", encoding="utf-8") as handle:
        handle.write("two\n")
    live = await watcher.read()
    live_event = live.events[0]
    live_record = event_to_record(live_event, participant_id="p", source_epoch="epoch")

    with path.open("a", encoding="utf-8") as handle:
        handle.write("three\n")
    fresh = TranscriptSource(LegacyCountingObserver(path), cwd=str(tmp_path))
    newest = await fresh.history_page(limit=1)
    assert newest.older_cursor is not None
    older = await fresh.history_page(before=newest.older_cursor, limit=1)
    historical_event = older.events[0]
    historical_record = event_to_record(historical_event, participant_id="p", source_epoch="epoch")
    assert live_event.raw_index == historical_event.raw_index
    assert live_event.source_offset == historical_event.source_offset
    assert live_record.record_id == historical_record.record_id


async def test_history_page_does_not_retain_unbounded_event_raw_text(tmp_path) -> None:
    path = tmp_path / "raw.jsonl"
    path.write_text("record\n", encoding="utf-8")
    observer = RawObserver(path)
    source = TranscriptSource(observer, cwd=str(tmp_path))
    attached = await source.read()
    assert attached.attached is not None
    source.commit_attachment()
    page = await source.history_page(limit=1)
    assert page.events[0].raw_text is not None
    assert page.events[0].raw_text.startswith("[raw]\\literal")
    assert len(page.events[0].raw_text.encode("utf-8")) <= 16 * 1024


class LegacySource(Source):
    async def read(self) -> Batch:
        return Batch(events=(Event(kind=EventKind.USER, text="legacy"),))


class OldStyleObserver:
    def __init__(self) -> None:
        self.source = LegacySource()

    def open_source(
        self, *, cwd: str | None, session_id: str | None = None, after: float | None = None
    ):
        return self.source


def test_old_style_observer_and_source_remain_compatible() -> None:
    observer = OldStyleObserver()
    assert (
        open_participant_source(
            observer,
            participant_id="p",
            cwd=None,
        )
        is observer.source
    )


class HistoryOnlySource(Source):
    async def read(self) -> Batch:
        return Batch()

    async def history(self, *, last_n: int) -> History:
        return History(events=(Event(kind=EventKind.USER, text="history"),), pinned=True)


class UnboundedHistorySource(Source):
    async def read(self) -> Batch:
        return Batch()

    async def history(self, *, last_n: int) -> History:
        return History(
            events=tuple(
                Event(kind=EventKind.ASSISTANT, text="x" * 20_000, raw_text="y" * 20_000)
                for _ in range(500)
            )
        )


async def test_default_history_page_is_honest_about_older_history() -> None:
    page = await HistoryOnlySource().history_page(limit=2)
    assert [event.text for event in page.events] == ["history"]
    assert page.pinned is True
    assert page.has_older is False
    assert page.older_cursor is None
    unavailable = await HistoryOnlySource().history_page(before="opaque", limit=2)
    assert unavailable.error_code == "history_paging_unavailable"
    assert unavailable.has_older is False


async def test_default_history_page_bounds_legacy_history() -> None:
    page = await UnboundedHistorySource().history_page(limit=2)
    assert len(page.events) == 2
    assert all(len(event.text.encode("utf-8")) <= 16 * 1024 for event in page.events)
    assert all(event.raw_text is not None for event in page.events)
    assert all(len(event.raw_text.encode("utf-8")) <= 16 * 1024 for event in page.events)


def test_panel_state_wire_round_trip() -> None:
    page = TrajectoryPage(panel_state=PanelStateInfo(PanelState.WAITING, "waiting for transcript"))
    assert TrajectoryPage.from_wire(page.to_wire()) == page
