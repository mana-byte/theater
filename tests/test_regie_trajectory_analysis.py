from __future__ import annotations

import json

import pytest

from theater.regie.trajectory.analysis import build_analysis_index
from theater.regie.trajectory.enums import DiagnosticView
from theater.regie.trajectory.render.requests import build_request_index
from theater.regie.trajectory.render.tools import build_tool_index
from theater.regie.trajectory.widgets.insights import build_insight_table
from theater.trajectory import (
    ContentFormat,
    CostProvenance,
    DetailField,
    LinkDirection,
    ParticipantLink,
    Timing,
    TimingProvenance,
    TrajectoryFailure,
    TrajectoryFailureCategory,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryStatus,
    TrajectoryUsage,
)


def _record(
    record_id: str,
    index: int,
    *,
    lane: TrajectoryLane = TrajectoryLane.MODEL,
    kind: TrajectoryKind = TrajectoryKind.ASSISTANT,
    status: TrajectoryStatus = TrajectoryStatus.COMPLETED,
    request_id: str | None = None,
    call_id: str | None = None,
    parent_call_id: str | None = None,
    timing: Timing | None = None,
    usage: TrajectoryUsage | None = None,
    details: tuple[DetailField, ...] = (),
    links: tuple[ParticipantLink, ...] = (),
    failure: TrajectoryFailure | None = None,
    retry_of_record_id: str | None = None,
    retry_attempt: int | None = None,
    turn_id: str | None = "turn-1",
) -> TrajectoryRecord:
    return TrajectoryRecord(
        record_id=record_id,
        revision=1,
        participant_id="p1",
        source_epoch="epoch",
        lane=lane,
        kind=kind,
        source="codex",
        summary=record_id,
        status=status,
        raw_index=index,
        turn_id=turn_id,
        request_id=request_id,
        call_id=call_id,
        parent_call_id=parent_call_id,
        timing=timing,
        usage=usage,
        details=details,
        links=links,
        failure=failure,
        retry_of_record_id=retry_of_record_id,
        retry_attempt=retry_attempt,
    )


def _index(records: tuple[TrajectoryRecord, ...]):
    requests = build_request_index(records)
    tools = build_tool_index(records)
    return build_analysis_index(records, requests, tools)


def test_analysis_projects_nested_waterfall_and_only_structured_file_paths() -> None:
    records = (
        _record(
            "model",
            1,
            request_id="req",
            timing=Timing(1, 6, 5_000, TimingProvenance.SOURCE, first_token=2),
        ),
        _record(
            "parent-call",
            2,
            lane=TrajectoryLane.TOOLS,
            kind=TrajectoryKind.TOOL_CALL,
            request_id="req",
            call_id="parent",
            timing=Timing(start=2, provenance=TimingProvenance.SOURCE),
            details=(
                DetailField.from_text(
                    "arguments",
                    json.dumps(
                        {
                            "file_path": "src/visible.py",
                            "command": "cat src/must-not-be-inferred.py",
                        }
                    ),
                    format=ContentFormat.JSON,
                ),
            ),
        ),
        _record(
            "child-call",
            3,
            lane=TrajectoryLane.TOOLS,
            kind=TrajectoryKind.TOOL_CALL,
            request_id="req",
            call_id="child",
            parent_call_id="parent",
            timing=Timing(start=3, provenance=TimingProvenance.SOURCE),
            details=(
                DetailField.from_text("path.write", "src/explicit.py", format=ContentFormat.PATH),
            ),
        ),
        _record(
            "child-result",
            4,
            lane=TrajectoryLane.TOOLS,
            kind=TrajectoryKind.TOOL_RESULT,
            request_id="req",
            call_id="child",
            timing=Timing(end=4, provenance=TimingProvenance.SOURCE),
        ),
        _record(
            "parent-result",
            5,
            lane=TrajectoryLane.TOOLS,
            kind=TrajectoryKind.TOOL_RESULT,
            request_id="req",
            call_id="parent",
            timing=Timing(end=5, provenance=TimingProvenance.SOURCE),
        ),
    )

    index = _index(records)
    waterfall = index.waterfalls[0]

    assert [(row.label, row.depth) for row in waterfall.rows] == [
        ("model unknown", 0),
        ("parent-call", 1),
        ("child-call", 2),
    ]
    assert waterfall.start == 1 and waterfall.end == 6 and waterfall.first_token == 2
    waterfall_table = build_insight_table(
        DiagnosticView.WATERFALL,
        index,
        frozenset(record.record_id for record in records),
    )
    assert [entry.row_height for entry in waterfall_table.entries] == [1, 1, 1]
    assert {row.path: row.modes for row in index.files} == {
        "src/explicit.py": frozenset({"write"}),
        "src/visible.py": frozenset({"reference"}),
    }
    assert all("must-not-be-inferred" not in row.path for row in index.files)


def test_waterfall_aggregates_model_requests_by_canonical_turn() -> None:
    records = (
        _record(
            "model-1",
            1,
            request_id="request-1",
            timing=Timing(1, 2, 1_000, TimingProvenance.SOURCE),
            usage=TrajectoryUsage(model="claude", input_tokens=10),
        ),
        _record(
            "tool-call",
            2,
            lane=TrajectoryLane.TOOLS,
            kind=TrajectoryKind.TOOL_CALL,
            request_id="request-1",
            call_id="tool",
            timing=Timing(start=2, provenance=TimingProvenance.SOURCE),
        ),
        _record(
            "tool-result",
            3,
            lane=TrajectoryLane.TOOLS,
            kind=TrajectoryKind.TOOL_RESULT,
            request_id="request-1",
            call_id="tool",
            timing=Timing(end=3, provenance=TimingProvenance.SOURCE),
        ),
        _record(
            "model-2",
            4,
            request_id="request-2",
            timing=Timing(3, 5, 2_000, TimingProvenance.SOURCE),
            usage=TrajectoryUsage(model="claude", output_tokens=20),
        ),
    )

    index = _index(records)

    assert len(index.waterfalls) == 1
    waterfall = index.waterfalls[0]
    assert waterfall.turn_id == "turn-1"
    assert waterfall.label == "claude · 2 calls"
    assert [(row.label, row.scope) for row in waterfall.rows] == [
        ("claude · 2 calls", True),
        ("tool-call", False),
    ]
    assert index.waterfall_for("model-1") is waterfall
    assert index.waterfall_for("model-2") is waterfall


def test_waterfall_keeps_request_scopes_separate_without_a_turn_identity() -> None:
    records = (
        _record("model-1", 1, request_id="request-1", turn_id=None),
        _record("model-2", 2, request_id="request-2", turn_id=None),
    )

    assert len(_index(records).waterfalls) == 2


def test_waterfall_uses_turn_records_when_harness_has_no_request_identity() -> None:
    records = (
        _record("answer", 1),
        _record(
            "tool-call",
            2,
            lane=TrajectoryLane.TOOLS,
            kind=TrajectoryKind.TOOL_CALL,
            call_id="tool",
            timing=Timing(start=2, provenance=TimingProvenance.OBSERVED),
        ),
        _record(
            "tool-result",
            3,
            lane=TrajectoryLane.TOOLS,
            kind=TrajectoryKind.TOOL_RESULT,
            call_id="tool",
            timing=Timing(duration_ms=250, provenance=TimingProvenance.SOURCE),
        ),
    )

    index = _index(records)

    assert not build_request_index(records).ordered
    assert len(index.waterfalls) == 1
    waterfall = index.waterfalls[0]
    assert waterfall.turn_id == "turn-1"
    assert waterfall.label == "model activity"
    assert waterfall.record_ids == ("answer", "tool-call", "tool-result")
    assert [(row.label, row.scope) for row in waterfall.rows] == [
        ("model activity", True),
        ("tool-call", False),
    ]
    scope_timing = waterfall.rows[0].timing
    assert scope_timing is not None
    assert scope_timing.provenance is TimingProvenance.DERIVED
    # tool-call start=2, tool-result duration 250ms => end=2.25; min start=2, max end=2.25
    assert scope_timing.start == 2
    assert scope_timing.end == 2.25
    assert scope_timing.duration_ms == 250


def test_file_activity_preserves_every_operation_per_path_in_chronological_order() -> None:
    records = (
        _record(
            "read-call",
            1,
            lane=TrajectoryLane.TOOLS,
            kind=TrajectoryKind.TOOL_CALL,
            call_id="read",
            timing=Timing(start=1, provenance=TimingProvenance.SOURCE),
            details=(
                DetailField.from_text("tool", "read_file"),
                DetailField.from_text(
                    "arguments",
                    json.dumps({"file_path": "src/shared.py"}),
                    format=ContentFormat.JSON,
                ),
            ),
        ),
        _record(
            "read-result",
            2,
            lane=TrajectoryLane.TOOLS,
            kind=TrajectoryKind.TOOL_RESULT,
            call_id="read",
            timing=Timing(end=2, provenance=TimingProvenance.SOURCE),
        ),
        _record(
            "patch-call",
            3,
            lane=TrajectoryLane.TOOLS,
            kind=TrajectoryKind.TOOL_CALL,
            call_id="patch",
            timing=Timing(start=3, provenance=TimingProvenance.SOURCE),
            details=(
                DetailField.from_text("tool", "apply_patch"),
                DetailField.from_text(
                    "arguments",
                    json.dumps({"paths": ["src/shared.py", "src/other.py"]}),
                    format=ContentFormat.JSON,
                ),
            ),
        ),
        _record(
            "patch-result",
            4,
            lane=TrajectoryLane.TOOLS,
            kind=TrajectoryKind.TOOL_RESULT,
            call_id="patch",
            timing=Timing(end=4, provenance=TimingProvenance.SOURCE),
        ),
    )

    files = {activity.path: activity for activity in _index(records).files}
    shared = files["src/shared.py"]
    other = files["src/other.py"]

    assert shared.operation_count == 2
    assert [operation.record_id for operation in shared.operations] == [
        "read-call",
        "patch-call",
    ]
    assert [operation.tool_name for operation in shared.operations] == [
        "read_file",
        "apply_patch",
    ]
    assert [operation.modes for operation in shared.operations] == [
        frozenset({"read"}),
        frozenset({"write"}),
    ]
    assert other.operation_count == 1
    assert other.operations[0].operation_id == shared.operations[1].operation_id
    assert other.operations[0].record_id == "patch-call"


def test_analysis_projects_delegation_resources_failures_and_retry_chains() -> None:
    reported = TrajectoryUsage(
        model="one",
        input_tokens=100,
        output_tokens=20,
        cost_usd=0.2,
        cost_provenance=CostProvenance.REPORTED,
    )
    estimated = TrajectoryUsage(
        model="two",
        input_tokens=50,
        cache_read_tokens=30,
        cost_usd=0.1,
        cost_provenance=CostProvenance.ESTIMATED,
    )
    records = (
        _record("request-one", 1, request_id="one", usage=reported),
        _record("request-two", 2, request_id="two", usage=estimated),
        _record(
            "failure",
            3,
            kind=TrajectoryKind.ERROR,
            status=TrajectoryStatus.ERROR,
            failure=TrajectoryFailure(
                TrajectoryFailureCategory.PROVIDER,
                code="rate_limit",
                detail="try later",
            ),
        ),
        _record(
            "retry",
            4,
            retry_of_record_id="failure",
            retry_attempt=2,
        ),
        _record(
            "send",
            5,
            lane=TrajectoryLane.THEATER,
            kind=TrajectoryKind.SEND,
            links=(
                ParticipantLink(
                    "p2",
                    "child",
                    LinkDirection.OUTGOING,
                    target_record_id="target",
                ),
            ),
        ),
    )

    index = _index(records)
    turn = next(row for row in index.resources if row.scope == "turn")

    assert turn.values.total_tokens == 200
    assert turn.values.cost_usd == pytest.approx(0.3)
    assert turn.values.cost_provenance == "mixed"
    assert turn.values.cost_complete
    assert [(row.record_id, row.chain_depth) for row in index.problems] == [
        ("failure", 0),
        ("retry", 1),
    ]
    assert index.delegations[0].target.target_record_id == "target"

    visible = frozenset(record.record_id for record in records)
    resource_table = build_insight_table(DiagnosticView.RESOURCES, index, visible)
    error_table = build_insight_table(DiagnosticView.ERRORS, index, visible)
    delegation_table = build_insight_table(DiagnosticView.DELEGATION, index, visible)

    assert len(resource_table.entries) == 3
    assert [entry.row_height for entry in resource_table.entries] == [2, 1, 1]
    assert "mixed" in str(resource_table.entries[0].cells[-1])
    assert len(error_table.entries) == 2
    assert error_table.row_height == 1
    assert error_table.entries[1].record_id == "retry"
    assert delegation_table.row_height == 2
    assert delegation_table.entries[0].link is not None
    assert delegation_table.entries[0].link.target_record_id == "target"


def test_insight_tables_use_requested_row_heights() -> None:
    index = _index(())
    visible: frozenset[str] = frozenset()

    assert build_insight_table(DiagnosticView.WATERFALL, index, visible).row_height == 1
    assert build_insight_table(DiagnosticView.RESOURCES, index, visible).row_height == 1
    assert build_insight_table(DiagnosticView.FILES, index, visible).row_height == 1
