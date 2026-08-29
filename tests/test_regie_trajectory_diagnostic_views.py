from __future__ import annotations

from theater.constants.trajectory import TRAJECTORY_MAX_GROUP_RECORD_IDS
from theater.regie.trajectory.enums import DiagnosticView
from theater.regie.trajectory.render.diagnostics import (
    build_diagnostic_index,
    ordering_for_projection,
)
from theater.regie.trajectory.render.requests import build_request_index
from theater.regie.trajectory.render.tools import build_tool_index
from theater.regie.trajectory.search import search_records
from theater.trajectory import (
    Timing,
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
    summary: str | None = None,
    call_id: str | None = None,
    request_id: str | None = None,
    timing: Timing | None = None,
    source_epoch: str = "epoch",
) -> TrajectoryRecord:
    return TrajectoryRecord(
        record_id=record_id,
        revision=1,
        participant_id="p1",
        source_epoch=source_epoch,
        lane=lane,
        kind=kind,
        source="codex",
        summary=summary or record_id,
        status=status,
        raw_index=index,
        call_id=call_id,
        request_id=request_id,
        timing=timing,
        usage=TrajectoryUsage(request_id=request_id) if request_id is not None else None,
    )


def _index(records: tuple[TrajectoryRecord, ...]):
    requests = build_request_index(records)
    tools = build_tool_index(records)
    return build_diagnostic_index(records, requests, tools), requests, tools


def test_all_running_errors_tools_and_coordination_are_exact_cached_projections() -> None:
    records = (
        _record("done", 1),
        _record("running", 2, status=TrajectoryStatus.RUNNING),
        _record(
            "unmatched-call",
            3,
            lane=TrajectoryLane.TOOLS,
            kind=TrajectoryKind.TOOL_CALL,
            call_id="open",
        ),
        _record(
            "error-call",
            4,
            lane=TrajectoryLane.TOOLS,
            kind=TrajectoryKind.TOOL_CALL,
            call_id="failed",
        ),
        _record(
            "error-result",
            5,
            lane=TrajectoryLane.TOOLS,
            kind=TrajectoryKind.TOOL_RESULT,
            status=TrajectoryStatus.ERROR,
            call_id="failed",
        ),
        _record(
            "theater-send",
            6,
            lane=TrajectoryLane.THEATER,
            kind=TrajectoryKind.SEND,
        ),
        _record(
            "bus:7",
            7,
            lane=TrajectoryLane.THEATER,
            kind=TrajectoryKind.AWAIT_START,
            source_epoch="theater-bus",
        ),
        _record(
            "bus:native",
            8,
            lane=TrajectoryLane.THEATER,
            kind=TrajectoryKind.THEATER_CALL,
        ),
    )
    index, _requests, tools = _index(records)

    assert index.projection_for(DiagnosticView.ALL).record_ids == {
        record.record_id for record in records if record.record_id != "bus:7"
    }
    assert index.projection_for(DiagnosticView.RUNNING).record_ids == {
        "running",
        "unmatched-call",
    }
    assert index.projection_for(DiagnosticView.ERRORS).record_ids == {
        "error-call",
        "error-result",
    }
    assert index.projection_for(DiagnosticView.TOOLS).record_ids == {
        "unmatched-call",
        "error-call",
        "error-result",
    }
    assert index.projection_for(DiagnosticView.COORDINATION).record_ids == {
        "theater-send",
        "bus:7",
        "bus:native",
    }

    tools_result = search_records(
        records,
        candidate_ids=index.projection_for(DiagnosticView.TOOLS).record_ids,
        tool_index=tools,
    )
    assert tools_result.row_ids == ("unmatched-call", "error-call")


def test_slow_orders_known_model_requests_and_logical_tools_by_duration() -> None:
    records = (
        _record(
            "model-slow",
            1,
            request_id="model-request",
            timing=Timing(duration_ms=500),
        ),
        _record(
            "tool-call",
            2,
            lane=TrajectoryLane.TOOLS,
            kind=TrajectoryKind.TOOL_CALL,
            call_id="tool",
            timing=Timing(start=1),
        ),
        _record(
            "tool-result",
            3,
            lane=TrajectoryLane.TOOLS,
            kind=TrajectoryKind.TOOL_RESULT,
            call_id="tool",
            timing=Timing(end=2),
        ),
        _record("model-fast", 4, timing=Timing(duration_ms=400)),
        _record("model-unknown", 5),
    )
    index, requests, tools = _index(records)
    projection = index.projection_for(DiagnosticView.SLOW)
    ordering = ordering_for_projection(records, projection)
    assert ordering is not None

    result = search_records(
        records,
        ordering=ordering,
        candidate_ids=projection.record_ids,
        request_index=requests,
        tool_index=tools,
    )

    assert projection.record_ids == {"tool-call", "tool-result", "model-slow", "model-fast"}
    assert result.row_ids == ("tool-call", "model-slow", "model-fast")
    assert "model-unknown" not in result.record_ids


def test_slow_order_chunks_large_projections_without_changing_rank() -> None:
    count = TRAJECTORY_MAX_GROUP_RECORD_IDS + 1
    records = tuple(
        _record(f"record-{index}", index, timing=Timing(duration_ms=float(index + 1)))
        for index in range(count)
    )
    index, _requests, _tools = _index(records)

    ordering = ordering_for_projection(
        records,
        index.projection_for(DiagnosticView.SLOW),
    )

    assert ordering is not None
    assert [len(group.record_ids) for group in ordering.groups] == [
        TRAJECTORY_MAX_GROUP_RECORD_IDS,
        1,
    ]
    assert [record.record_id for record in ordering.records] == [
        f"record-{index}" for index in reversed(range(count))
    ]


def test_diagnostic_candidates_compose_with_text_and_typed_filters() -> None:
    records = (
        _record("running-match", 1, status=TrajectoryStatus.RUNNING, summary="needle"),
        _record("running-other", 2, status=TrajectoryStatus.RUNNING, summary="other"),
        _record("completed-match", 3, summary="needle"),
    )
    index, _requests, _tools = _index(records)

    result = search_records(
        records,
        query="needle",
        status_filters=(TrajectoryStatus.RUNNING,),
        candidate_ids=index.projection_for(DiagnosticView.RUNNING).record_ids,
    )

    assert result.record_ids == ("running-match",)
