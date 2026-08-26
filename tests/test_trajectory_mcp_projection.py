"""Central projection of harness-decoded MCP operations."""

from __future__ import annotations

import pytest

from theater.daemon.trajectory.project import fact_to_record
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.trajectory import (
    TrajectoryFailure,
    TrajectoryFailureCategory,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryStatus,
    TrajectoryValidationError,
    tool_operations_for_records,
)


def _fact(
    kind: TrajectoryKind,
    *,
    status: TrajectoryStatus,
    server: str = "theater",
    failure: TrajectoryFailure | None = None,
) -> TrajectoryFact:
    return TrajectoryFact(
        kind=kind,
        lane=TrajectoryLane.TOOLS,
        source="harness",
        summary="theater.send",
        status=status,
        native_id=kind.value,
        call_id="call-1",
        mcp_server=server,
        mcp_tool="send",
        failure=failure,
    )


def test_theater_mcp_calls_are_projected_into_theater_activity() -> None:
    call = fact_to_record(
        _fact(TrajectoryKind.TOOL_CALL, status=TrajectoryStatus.RUNNING),
        participant_id="participant",
        source_epoch="epoch",
    )
    result = fact_to_record(
        _fact(TrajectoryKind.TOOL_RESULT, status=TrajectoryStatus.COMPLETED),
        participant_id="participant",
        source_epoch="epoch",
    )

    assert (call.kind, call.lane, call.summary) == (
        TrajectoryKind.THEATER_CALL,
        TrajectoryLane.THEATER,
        "send",
    )
    assert (result.kind, result.lane, result.summary) == (
        TrajectoryKind.THEATER_RESULT,
        TrajectoryLane.THEATER,
        "send completed",
    )
    assert TrajectoryRecord.from_wire(call.to_wire()) == call
    assert TrajectoryRecord.from_wire(result.to_wire()) == result
    assert tool_operations_for_records((call, result)) == ()


def test_theater_mcp_failure_moves_out_of_the_tool_failure_domain() -> None:
    record = fact_to_record(
        _fact(
            TrajectoryKind.TOOL_RESULT,
            status=TrajectoryStatus.ERROR,
            failure=TrajectoryFailure(
                TrajectoryFailureCategory.TOOL,
                code="bad_request",
                detail="rejected",
            ),
        ),
        participant_id="participant",
        source_epoch="epoch",
    )

    assert record.kind is TrajectoryKind.THEATER_RESULT
    assert record.summary == "send failed"
    assert record.failure == TrajectoryFailure(
        TrajectoryFailureCategory.THEATER,
        code="bad_request",
        detail="rejected",
    )


def test_non_theater_mcp_calls_remain_generic_tools() -> None:
    record = fact_to_record(
        _fact(
            TrajectoryKind.TOOL_CALL,
            status=TrajectoryStatus.RUNNING,
            server="github",
        ),
        participant_id="participant",
        source_epoch="epoch",
    )

    assert record.kind is TrajectoryKind.TOOL_CALL
    assert record.lane is TrajectoryLane.TOOLS
    assert record.summary == "theater.send"
    assert len(tool_operations_for_records((record,))) == 1


def test_mcp_identity_is_atomic_and_strict_on_wire() -> None:
    with pytest.raises(TrajectoryValidationError, match="requires both server and tool"):
        TrajectoryFact(kind=TrajectoryKind.TOOL_CALL, mcp_server="theater")

    record = fact_to_record(
        _fact(TrajectoryKind.TOOL_CALL, status=TrajectoryStatus.RUNNING),
        participant_id="participant",
        source_epoch="epoch",
    )
    wire = record.to_wire()
    wire.pop("mcp_tool")
    with pytest.raises(TrajectoryValidationError, match="requires both server and tool"):
        TrajectoryRecord.from_wire(wire)
