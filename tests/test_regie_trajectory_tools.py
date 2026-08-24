"""Focused logical tool-row coverage for the Régie trajectory ledger."""

from __future__ import annotations

from theater.regie.trajectory.details import tool_detail_text
from theater.regie.trajectory.enums import InspectorTab
from theater.regie.trajectory.pagination import paginate_search_result
from theater.regie.trajectory.search import search_records
from theater.regie.trajectory.tool_rows import build_tool_index, tool_row_text
from theater.trajectory import (
    DetailField,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryStatus,
)


def _tool(
    record_id: str,
    index: int,
    kind: TrajectoryKind,
    call_id: str,
    *,
    summary: str = "tool",
    details: tuple[DetailField, ...] = (),
) -> TrajectoryRecord:
    return TrajectoryRecord(
        record_id=record_id,
        revision=1,
        participant_id="participant",
        source_epoch="epoch",
        lane=TrajectoryLane.TOOLS,
        kind=kind,
        source="codex",
        summary=summary,
        status=TrajectoryStatus.COMPLETED,
        raw_index=index,
        call_id=call_id,
        details=details,
    )


def test_index_anchors_matched_operation_and_maps_every_member() -> None:
    call = _tool(
        "call", 1, TrajectoryKind.TOOL_CALL, "one", details=(DetailField.from_text("tool", "exec"),)
    )
    result = _tool("result", 2, TrajectoryKind.TOOL_RESULT, "one")

    index = build_tool_index((call, result))
    operation = index.ordered[0]

    assert index.anchor_by_id[operation.operation_id] == "call"
    assert index.by_record_id == {"call": operation.operation_id, "result": operation.operation_id}


def test_search_and_pagination_count_a_pair_as_one_logical_row() -> None:
    call = _tool("call", 1, TrajectoryKind.TOOL_CALL, "one", summary="invoke")
    result = _tool("result", 2, TrajectoryKind.TOOL_RESULT, "one", summary="finished")
    index = build_tool_index((call, result))

    search = search_records((call, result), query="finished", tool_index=index)
    page = paginate_search_result(search, 0, 1)

    assert search.record_ids == ("result",)
    assert search.row_ids == ("call",)
    assert search.row_id_for_record("result") == "call"
    assert page.total_items == 1
    assert page.result.row_ids == ("call",)
    assert page.result.row_id_for_record("result") == "call"


def test_unmatched_text_and_details_are_explicit() -> None:
    call = _tool(
        "call", 1, TrajectoryKind.TOOL_CALL, "one", details=(DetailField.from_text("tool", "exec"),)
    )
    operation = build_tool_index((call,)).ordered[0]

    assert "awaiting result" in tool_row_text(operation).summary
    assert tool_detail_text(operation, tab=InspectorTab.RESULT) == "No result supplied."
