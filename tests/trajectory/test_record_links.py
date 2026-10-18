"""Record causality links, exact record locations and MCP operation projection."""

from __future__ import annotations

import pytest

from theater.constants.trajectory import (
    TRAJECTORY_IDENTIFIER_MAX_BYTES,
    TRAJECTORY_OVERVIEW_SUMMARY_MAX_BYTES,
)
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
from theater.trajectory.causality import CausalIndex, CausalResolution
from theater.trajectory.enums import LinkDirection
from theater.trajectory.location import TrajectoryLocation, TrajectoryLocationResolution
from theater.trajectory.records import ParticipantLink


def _record(
    participant_id: str,
    record_id: str,
    *,
    raw_index: int = 0,
    links: tuple[ParticipantLink, ...] = (),
) -> TrajectoryRecord:
    return TrajectoryRecord(
        record_id=record_id,
        revision=0,
        participant_id=participant_id,
        source_epoch="theater-bus",
        lane=TrajectoryLane.THEATER,
        kind=TrajectoryKind.THEATER,
        source="theater",
        summary="coordination",
        status=TrajectoryStatus.COMPLETED,
        raw_index=raw_index,
        source_offset=raw_index,
        links=links,
    )


def test_participant_link_old_and_new_wire_forms_round_trip() -> None:
    old = {"participant_id": "child", "relation": "recipient", "direction": "outgoing"}
    assert ParticipantLink.from_wire(old).to_wire() == old

    link = ParticipantLink(
        "child",
        "recipient",
        LinkDirection.OUTGOING,
        target_record_id="bus:17",
        correlation_type="job_handle",
        correlation_key="child#3",
    )
    assert ParticipantLink.from_wire(link.to_wire()) == link


@pytest.mark.parametrize(
    "kwargs",
    (
        {"target_record_id": "x" * (TRAJECTORY_IDENTIFIER_MAX_BYTES + 1)},
        {"correlation_type": "job_handle"},
        {"correlation_key": "child#3"},
        {"correlation_type": "timestamp", "correlation_key": "123"},
        {
            "correlation_type": "job_handle",
            "correlation_key": "x" * (TRAJECTORY_IDENTIFIER_MAX_BYTES + 1),
        },
    ),
)
def test_participant_link_rejects_invalid_or_oversized_causality_fields(
    kwargs: dict[str, str],
) -> None:
    with pytest.raises(ValueError):
        ParticipantLink.from_wire(
            {
                "participant_id": "child",
                "relation": "recipient",
                **kwargs,
            }
        )


def test_index_distinguishes_exact_participant_only_and_unresolved_links() -> None:
    target = _record("child", "bus:7", raw_index=7)
    source = _record(
        "parent",
        "bus:7",
        raw_index=7,
        links=(
            ParticipantLink("child", "exact", target_record_id="bus:7"),
            ParticipantLink("child", "participant"),
            ParticipantLink("missing", "unresolved", target_record_id="bus:99"),
        ),
    )
    resolutions = CausalIndex((source, target)).links_for(source)

    assert [item.resolution for item in resolutions] == [
        CausalResolution.EXACT,
        CausalResolution.PARTICIPANT,
        CausalResolution.UNRESOLVED,
    ]
    assert resolutions[0].target == target
    assert resolutions[1].target is None
    assert resolutions[2].target is None


def test_index_returns_exact_counterpart_and_stable_handle_chain_in_bus_order() -> None:
    parent_send = _record(
        "parent",
        "bus:12",
        raw_index=12,
        links=(
            ParticipantLink(
                "child",
                "recipient",
                target_record_id="bus:12",
                correlation_type="job_handle",
                correlation_key="child#2",
            ),
        ),
    )
    child_receive = _record(
        "child",
        "bus:12",
        raw_index=12,
        links=(
            ParticipantLink(
                "parent",
                "sender",
                target_record_id="bus:12",
                correlation_type="job_handle",
                correlation_key="child#2",
            ),
        ),
    )
    await_end = _record(
        "parent",
        "bus:20",
        raw_index=20,
        links=(
            ParticipantLink(
                "child",
                "recipient",
                target_record_id="bus:20",
                correlation_type="job_handle",
                correlation_key="child#2",
            ),
        ),
    )
    reply = _record(
        "child",
        "bus:30",
        raw_index=30,
        links=(
            ParticipantLink(
                "parent",
                "recipient",
                target_record_id="bus:30",
                correlation_type="job_handle",
                correlation_key="child#2",
            ),
        ),
    )
    index = CausalIndex((reply, child_receive, await_end, parent_send))

    assert index.resolve(parent_send.links[0]).target == child_receive
    assert [
        (record.participant_id, record.record_id) for record in index.related_records(parent_send)
    ] == [
        ("child", "bus:12"),
        ("parent", "bus:20"),
        ("child", "bus:30"),
    ]


def _plain_record(*, participant_id: str = "p", record_id: str = "record") -> TrajectoryRecord:
    return TrajectoryRecord(
        record_id=record_id,
        revision=0,
        participant_id=participant_id,
        source_epoch="epoch",
        lane=TrajectoryLane.MODEL,
        kind=TrajectoryKind.ASSISTANT,
        source="test",
        summary="answer",
        status=TrajectoryStatus.COMPLETED,
    )


def test_exact_location_round_trips_and_requires_matching_record() -> None:
    record = _plain_record()
    location = TrajectoryLocation("p", "record", TrajectoryLocationResolution.EXACT, record)

    assert TrajectoryLocation.from_wire(location.to_wire()) == location
    with pytest.raises(ValueError, match="must match participant and requested record"):
        TrajectoryLocation("other", "record", TrajectoryLocationResolution.EXACT, record)
    with pytest.raises(ValueError, match="requires a record"):
        TrajectoryLocation("p", "record", TrajectoryLocationResolution.EXACT)


def test_non_exact_location_has_no_record_and_wire_is_strict_and_bounded() -> None:
    record = _plain_record()
    with pytest.raises(ValueError, match="must not contain a record"):
        TrajectoryLocation("p", "record", TrajectoryLocationResolution.NOT_FOUND, record)
    with pytest.raises(ValueError):
        TrajectoryLocation(
            "p",
            "record",
            TrajectoryLocationResolution.UNAVAILABLE,
            message="x" * (TRAJECTORY_OVERVIEW_SUMMARY_MAX_BYTES + 1),
        )
    with pytest.raises(ValueError):
        TrajectoryLocation(
            "x" * (TRAJECTORY_IDENTIFIER_MAX_BYTES + 1),
            "record",
            TrajectoryLocationResolution.NOT_FOUND,
        )
    with pytest.raises(ValueError):
        TrajectoryLocation.from_wire(
            {
                "participant_id": "p",
                "requested_record_id": "record",
                "resolution": "not_found",
                "record": None,
                "message": "missing",
                "extra": True,
            }
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


@pytest.mark.parametrize("server", ["theater", "theater_wait"])
def test_a_refusal_delivered_as_text_is_still_an_error(server: str) -> None:
    text = 'REFUSED busy: working\n{"ok":false,"error":{"code":"busy","message":"working"}}'
    fact = TrajectoryFact(
        kind=TrajectoryKind.TOOL_RESULT,
        lane=TrajectoryLane.TOOLS,
        summary=text,
        status=TrajectoryStatus.COMPLETED,
        native_id="result",
        call_id="call-1",
        mcp_server=server,
        mcp_tool="send",
    )
    record = fact_to_record(fact, participant_id="participant", source_epoch="epoch")
    assert record.status is TrajectoryStatus.ERROR
    assert record.failure is not None and record.failure.code == "busy"
