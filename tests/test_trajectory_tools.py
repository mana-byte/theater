"""Focused tests for exact trajectory tool operation projections."""

from __future__ import annotations

from pathlib import Path

import pytest

from theater.constants.trajectory import (
    TRAJECTORY_IDENTIFIER_MAX_BYTES,
    TRAJECTORY_SOURCE_MAX_BYTES,
    TRAJECTORY_TOOL_RECORD_LIMIT,
)
from theater.daemon.trajectory.project import fact_to_record
from theater.harness.builtin.plugins.claude import ClaudeCodeObserver
from theater.harness.builtin.plugins.codex import CodexObserver
from theater.trajectory import (
    ContentPreview,
    DetailField,
    Timing,
    TimingProvenance,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryStatus,
    TrajectoryToolIdentity,
    TrajectoryToolOperation,
    TrajectoryValidationError,
    tool_operations_for_records,
)
from theater.trajectory.enums import TrajectoryFailureCategory
from theater.trajectory.records import TrajectoryFailure

FIXTURES = Path(__file__).parent / "fixtures"


def _record(
    record_id: str,
    index: int,
    *,
    kind: TrajectoryKind,
    call_id: str | None = None,
    participant_id: str = "participant",
    source_epoch: str = "epoch",
    source: str = "test",
    summary: str = "summary",
    status: TrajectoryStatus = TrajectoryStatus.COMPLETED,
    revision: int = 0,
    request_id: str | None = None,
    parent_call_id: str | None = None,
    timing: Timing | None = None,
    details: tuple[DetailField, ...] = (),
    failure: TrajectoryFailure | None = None,
    retry_of_record_id: str | None = None,
    retry_attempt: int | None = None,
) -> TrajectoryRecord:
    return TrajectoryRecord(
        record_id=record_id,
        revision=revision,
        participant_id=participant_id,
        source_epoch=source_epoch,
        lane=TrajectoryLane.TOOLS,
        kind=kind,
        source=source,
        summary=summary,
        status=status,
        raw_index=index,
        call_id=call_id,
        request_id=request_id,
        parent_call_id=parent_call_id,
        timing=timing,
        details=details,
        failure=failure,
        retry_of_record_id=retry_of_record_id,
        retry_attempt=retry_attempt,
    )


def _call(record_id: str, index: int, call_id: str | None, **kwargs: object) -> TrajectoryRecord:
    return _record(record_id, index, kind=TrajectoryKind.TOOL_CALL, call_id=call_id, **kwargs)


def _result(record_id: str, index: int, call_id: str | None, **kwargs: object) -> TrajectoryRecord:
    return _record(record_id, index, kind=TrajectoryKind.TOOL_RESULT, call_id=call_id, **kwargs)


def _fixture_records(observer, name: str) -> list[TrajectoryRecord]:
    return [
        fact_to_record(fact, participant_id="participant", source_epoch="epoch")
        for index, line in enumerate((FIXTURES / name).read_text(encoding="utf-8").splitlines())
        for fact in observer.parse_record(line, index).trajectory
    ]


def test_exact_pair_uses_last_records_for_display_data() -> None:
    call = _call(
        "call",
        1,
        "id",
        summary="fallback",
        status=TrajectoryStatus.RUNNING,
        details=(DetailField.from_text("tool", "exec"), DetailField.from_text("input", "pwd")),
        timing=Timing(start=1, provenance=TimingProvenance.SOURCE),
    )
    result = _result(
        "result",
        2,
        "id",
        status=TrajectoryStatus.COMPLETED,
        details=(DetailField.from_text("result", "ok"),),
        timing=Timing(end=2, provenance=TimingProvenance.SOURCE),
    )

    operation = tool_operations_for_records((call, result))[0]

    assert operation.identity is TrajectoryToolIdentity.MATCHED
    assert operation.call_record_ids == ("call",)
    assert operation.result_record_ids == ("result",)
    assert operation.tool_name == "exec"
    assert operation.call_details == call.details
    assert operation.result_details == result.details
    assert operation.status is TrajectoryStatus.COMPLETED
    assert operation.timing == Timing(
        start=1, end=2, duration_ms=1000, provenance=TimingProvenance.DERIVED
    )


def test_tool_projection_preserves_explicit_failure_and_retry_link() -> None:
    failure = TrajectoryFailure(
        TrajectoryFailureCategory.TOOL,
        code="exit_1",
        detail="command failed",
    )
    operation = tool_operations_for_records(
        (
            _call("call", 1, "id", status=TrajectoryStatus.RUNNING),
            _result(
                "result",
                2,
                "id",
                status=TrajectoryStatus.ERROR,
                failure=failure,
                retry_of_record_id="prior",
                retry_attempt=2,
            ),
        )
    )[0]

    assert operation.failure == failure
    assert operation.retry_of_record_id == "prior"
    assert operation.retry_attempt == 2
    assert TrajectoryToolOperation.from_wire(operation.to_wire()) == operation


def test_pairing_is_exactly_scoped_and_never_heuristic() -> None:
    operations = tool_operations_for_records(
        (
            _call("p1", 1, "same", summary="exec"),
            _result("p2", 2, "same", participant_id="other", summary="exec"),
            _result("e2", 3, "same", source_epoch="other", summary="exec"),
            _call("different-call", 4, "call", summary="exec"),
            _result("different-result", 5, "result", summary="exec"),
        )
    )

    assert [operation.identity for operation in operations] == [
        TrajectoryToolIdentity.CALL_ONLY,
        TrajectoryToolIdentity.RESULT_ONLY,
        TrajectoryToolIdentity.CALL_ONLY,
        TrajectoryToolIdentity.RESULT_ONLY,
        TrajectoryToolIdentity.RESULT_ONLY,
    ]
    assert [operation.call_id for operation in operations] == [
        "same",
        "same",
        "call",
        "result",
        "same",
    ]


def test_unmatched_and_unkeyed_records_stay_explicit_and_separate() -> None:
    operations = tool_operations_for_records(
        (
            _call("call-only", 1, "call"),
            _result("result-only", 2, "result"),
            _call("unkeyed-call-1", 3, None),
            _call("unkeyed-call-2", 4, None),
            _result("unkeyed-result-1", 5, None),
            _result("unkeyed-result-2", 6, None),
        )
    )

    assert [operation.identity for operation in operations] == [
        TrajectoryToolIdentity.CALL_ONLY,
        TrajectoryToolIdentity.RESULT_ONLY,
        TrajectoryToolIdentity.UNKEYED_CALL,
        TrajectoryToolIdentity.UNKEYED_CALL,
        TrajectoryToolIdentity.UNKEYED_RESULT,
        TrajectoryToolIdentity.UNKEYED_RESULT,
    ]
    assert len({operation.operation_id for operation in operations}) == 6


def test_result_first_revisions_and_repeated_records_are_deterministic() -> None:
    result = _result("result", 1, "id")
    old = _call("call", 2, "id", revision=1, summary="old")
    new = _call("call", 2, "id", revision=2, summary="new")
    extra = _call("call-2", 3, "id")

    operation = tool_operations_for_records((old, result, new, extra))[0]

    assert operation.result_record_ids == ("result",)
    assert operation.call_record_ids == ("call", "call-2")
    assert operation.call_count == 2
    assert operation.result_count == 1
    assert operation.tool_name == "summary"


def test_request_parent_and_exact_child_links_are_conservative() -> None:
    parent = _call("parent", 1, "parent")
    child = _call("child", 2, "child", parent_call_id="parent", request_id="request")
    same_child = _call("same-child", 3, "child", parent_call_id="parent", request_id="other")
    other_epoch = _call("other", 4, "other", source_epoch="other", parent_call_id="parent")
    result = _result("result", 5, "child", parent_call_id="child", request_id="request")

    parent_operation, child_operation, *_ = tool_operations_for_records(
        (parent, child, same_child, other_epoch, result)
    )

    assert parent_operation.child_call_ids == ("child",)
    assert child_operation.request_id is None
    assert child_operation.parent_call_id == "parent"


def test_child_links_are_precomputed_exactly_per_parent_stream() -> None:
    parent = _call("parent", 1, "parent")
    first = _call("first", 2, "first", parent_call_id="parent")
    repeated = _call("repeated", 3, "first", parent_call_id="parent")
    second = _call("second", 4, "second", parent_call_id="parent")
    self_link = _call("self", 5, "parent", parent_call_id="parent")
    other_participant = _call("other-p", 6, "other-p", participant_id="other")
    other_participant_child = _call(
        "other-p-child", 7, "other-p-child", participant_id="other", parent_call_id="other-p"
    )
    other_epoch = _call("other-e", 8, "other-e", source_epoch="other")
    other_epoch_child = _call(
        "other-e-child", 9, "other-e-child", source_epoch="other", parent_call_id="other-e"
    )

    operations = tool_operations_for_records(
        (
            parent,
            first,
            repeated,
            second,
            self_link,
            other_participant,
            other_participant_child,
            other_epoch,
            other_epoch_child,
        )
    )
    children = {
        (
            operation.participant_id,
            operation.source_epoch,
            operation.call_id,
        ): operation.child_call_ids
        for operation in operations
    }

    assert children[("participant", "epoch", "parent")] == ("first", "second")
    assert children[("other", "epoch", "other-p")] == ("other-p-child",)
    assert children[("participant", "other", "other-e")] == ("other-e-child",)


def test_projector_retains_newest_record_ids_when_tool_operation_is_truncated() -> None:
    count = TRAJECTORY_TOOL_RECORD_LIMIT + 1
    records = tuple(_call(f"call-{index}", index, "shared") for index in range(count))

    operation = tool_operations_for_records(records)[0]

    assert operation.call_count == count
    assert operation.call_record_ids == tuple(f"call-{index}" for index in range(1, count))
    assert operation.records_truncated


def test_timing_derivation_fallback_contradictions_and_call_only() -> None:
    derived = tool_operations_for_records(
        (
            _call("a", 1, "derived", timing=Timing(start=2, provenance=TimingProvenance.SOURCE)),
            _call("b", 2, "derived", timing=Timing(start=1, provenance=TimingProvenance.OBSERVED)),
            _result("c", 3, "derived", timing=Timing(end=4, provenance=TimingProvenance.SOURCE)),
        )
    )[0]
    fallback = tool_operations_for_records(
        (
            _call("d", 1, "fallback", timing=Timing(start=4, provenance=TimingProvenance.SOURCE)),
            _result(
                "e",
                2,
                "fallback",
                timing=Timing(end=3, duration_ms=7, provenance=TimingProvenance.OBSERVED),
            ),
        )
    )[0]
    call_only = tool_operations_for_records(
        (_call("f", 1, "call-only", timing=Timing(start=1, end=9)),)
    )[0]
    running_call_only = tool_operations_for_records(
        (
            _call(
                "g",
                1,
                "running-call-only",
                status=TrajectoryStatus.RUNNING,
                timing=Timing(start=1, end=9),
            ),
        )
    )[0]

    assert derived.timing == Timing(
        start=1, end=4, duration_ms=3000, provenance=TimingProvenance.OBSERVED
    )
    assert fallback.timing == Timing(start=4, duration_ms=7, provenance=TimingProvenance.OBSERVED)
    assert call_only.timing == Timing(
        start=1, end=9, duration_ms=8000, provenance=TimingProvenance.DERIVED
    )
    assert running_call_only.timing == Timing(start=1, provenance=TimingProvenance.UNAVAILABLE)


def test_tool_timing_treats_terminal_result_point_as_operation_end() -> None:
    operation = tool_operations_for_records(
        (
            _call(
                "call",
                1,
                "shared",
                timing=Timing(start=10, provenance=TimingProvenance.SOURCE),
            ),
            _result(
                "result",
                2,
                "shared",
                timing=Timing(start=12, provenance=TimingProvenance.SOURCE),
            ),
        )
    )[0]

    assert operation.timing == Timing(
        start=10,
        end=12,
        duration_ms=2_000,
        provenance=TimingProvenance.DERIVED,
    )


def test_operation_ids_and_previews_are_bounded_and_details_preserve_omission() -> None:
    long = "x" * TRAJECTORY_IDENTIFIER_MAX_BYTES
    detail = DetailField("input", ContentPreview.from_text("x" * (20 * 1024)))
    record = _call(
        "record",
        1,
        long,
        participant_id=long,
        source_epoch=long,
        summary="y" * (TRAJECTORY_SOURCE_MAX_BYTES + 20),
        details=(detail,),
    )

    first = tool_operations_for_records((record,))[0]
    second = tool_operations_for_records((record,))[0]

    assert first.operation_id == second.operation_id
    assert len(first.operation_id.encode()) <= TRAJECTORY_IDENTIFIER_MAX_BYTES
    assert len(first.tool_name.encode()) <= TRAJECTORY_SOURCE_MAX_BYTES
    assert first.call_details[0].preview.omitted_bytes == detail.preview.omitted_bytes


def test_tool_wire_is_strict_and_round_trips_directly() -> None:
    operation = tool_operations_for_records((_call("call", 1, "id"), _result("result", 2, "id")))[0]
    invalid = operation.to_wire()
    invalid["extra"] = True
    missing = operation.to_wire()
    missing.pop("source")
    wrong = operation.to_wire()
    wrong["call_count"] = True
    missing_retained = operation.to_wire()
    missing_retained["identity"] = TrajectoryToolIdentity.RESULT_ONLY.value
    missing_retained["call_record_ids"] = []
    missing_retained["records_truncated"] = True
    zero_count = operation.to_wire()
    zero_count["call_count"] = 0

    wire = operation.to_wire()
    assert TrajectoryToolOperation.from_wire(wire) == operation
    assert TrajectoryToolOperation.from_wire(wire).to_wire() == wire
    with pytest.raises(TrajectoryValidationError):
        TrajectoryToolOperation.from_wire(invalid)
    with pytest.raises(TrajectoryValidationError):
        TrajectoryToolOperation.from_wire(missing)
    with pytest.raises(TrajectoryValidationError):
        TrajectoryToolOperation.from_wire(wrong)
    with pytest.raises(TrajectoryValidationError):
        TrajectoryToolOperation.from_wire(missing_retained)
    with pytest.raises(TrajectoryValidationError):
        TrajectoryToolOperation.from_wire(zero_count)
    with pytest.raises(TrajectoryValidationError):
        TrajectoryToolOperation.from_wire([])

    truncated = TrajectoryToolOperation(
        operation_id="tool",
        participant_id="participant",
        source_epoch="epoch",
        source="source",
        identity=TrajectoryToolIdentity.CALL_ONLY,
        call_id="call",
        call_record_ids=("newest",),
        result_record_ids=(),
        tool_name=None,
        status=TrajectoryStatus.RUNNING,
        call_count=2,
        result_count=0,
        records_truncated=True,
    )
    assert TrajectoryToolOperation.from_wire(truncated.to_wire()) == truncated


def test_input_is_not_mutated_and_supported_fixtures_pair_exact_ids() -> None:
    records = [_call("call", 2, "id"), _result("result", 1, "id")]
    original = list(records)
    claude = tool_operations_for_records(
        _fixture_records(ClaudeCodeObserver(), "trajectory_claude.jsonl")
    )
    codex = tool_operations_for_records(_fixture_records(CodexObserver(), "trajectory_codex.jsonl"))

    assert records == original
    assert next(operation for operation in claude if operation.call_id == "tool-1").identity is (
        TrajectoryToolIdentity.MATCHED
    )
    assert {
        operation.call_id: operation.identity
        for operation in codex
        if operation.call_id in {"call-1", "mcp-1", "unmatched-call"}
    } == {
        "call-1": TrajectoryToolIdentity.MATCHED,
        "mcp-1": TrajectoryToolIdentity.MATCHED,
        "unmatched-call": TrajectoryToolIdentity.RESULT_ONLY,
    }
