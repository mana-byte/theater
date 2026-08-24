"""Focused logical tool-row coverage for the Régie trajectory ledger."""

from __future__ import annotations

from theater.regie.trajectory.constants import (
    TOOL_ROW_SUMMARY_MAX_CHARS,
    TRAJECTORY_DETAIL_RECORD_MAX_BYTES,
)
from theater.regie.trajectory.details import build_tool_inline_details, tool_detail_text
from theater.regie.trajectory.enums import InspectorTab
from theater.regie.trajectory.pagination import paginate_search_result
from theater.regie.trajectory.search import search_records
from theater.regie.trajectory.tool_rows import build_tool_index, tool_row_text
from theater.trajectory import (
    ContentFormat,
    ContentPreview,
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
    call_id: str | None,
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


def _ordinary(record_id: str, index: int) -> TrajectoryRecord:
    return TrajectoryRecord(
        record_id=record_id,
        revision=1,
        participant_id="participant",
        source_epoch="epoch",
        lane=TrajectoryLane.MODEL,
        kind=TrajectoryKind.ASSISTANT,
        source="codex",
        summary=record_id,
        status=TrajectoryStatus.COMPLETED,
        raw_index=index,
    )


def test_page_record_ids_are_logical_and_adjacent_rows_paginate_once() -> None:
    first = _ordinary("first", 1)
    call = _tool("call", 2, TrajectoryKind.TOOL_CALL, "one")
    result = _tool("result", 3, TrajectoryKind.TOOL_RESULT, "one")
    last = _ordinary("last", 4)
    search = search_records(
        (first, call, result, last), tool_index=build_tool_index((call, result))
    )

    page_one = paginate_search_result(search, 0, 2)
    page_two = paginate_search_result(search, 1, 2)

    assert page_one.total_items == 3
    assert page_one.record_ids == ("first", "call")
    assert page_two.record_ids == ("last",)


def test_result_query_and_each_tool_kind_filter_keep_one_operation_row() -> None:
    call = _tool("call", 1, TrajectoryKind.TOOL_CALL, "one", summary="invoke")
    result = _tool("result", 2, TrajectoryKind.TOOL_RESULT, "one", summary="result-only")
    index = build_tool_index((call, result))

    assert search_records((call, result), query="result-only", tool_index=index).row_ids == (
        "call",
    )
    for kind in (TrajectoryKind.TOOL_CALL, TrajectoryKind.TOOL_RESULT):
        assert search_records((call, result), kind_filters=(kind,), tool_index=index).row_ids == (
            "call",
        )


def test_unmatched_tool_identities_stay_separate() -> None:
    records = (
        _tool("result", 1, TrajectoryKind.TOOL_RESULT, "key"),
        _tool("call", 2, TrajectoryKind.TOOL_CALL, None),
        _tool("unkeyed-result", 3, TrajectoryKind.TOOL_RESULT, None),
    )
    index = build_tool_index(records)

    assert len(index.ordered) == 3
    assert len(search_records(records, tool_index=index).row_ids) == 3
    assert "unmatched result" in tool_row_text(index.ordered[0]).summary


def test_tool_name_prefix_and_summary_bound() -> None:
    call = _tool(
        "call",
        1,
        TrajectoryKind.TOOL_CALL,
        "one",
        details=(DetailField.from_text("tool", "runner"),),
    )
    operation = build_tool_index((call,)).ordered[0]

    for compact in (False, True):
        text = tool_row_text(operation, compact=compact).summary
        assert text.startswith("[runner]")
        assert len(text) <= TOOL_ROW_SUMMARY_MAX_CHARS


def test_tool_details_bound_copy_and_show_omission() -> None:
    preview = ContentPreview(text='{"value":"' + "x" * 5000 + '"}', omitted_bytes=77)
    field = DetailField("result", preview, ContentFormat.JSON)
    result = _tool("result", 2, TrajectoryKind.TOOL_RESULT, "one", details=(field,))
    operation = build_tool_index((result,)).ordered[0]
    text = tool_detail_text(operation, InspectorTab.RESULT)
    detail = build_tool_inline_details(operation, InspectorTab.RESULT, max_height=1)

    assert len(text.encode()) <= TRAJECTORY_DETAIL_RECORD_MAX_BYTES
    assert "77 source bytes omitted" in text
    assert detail.copy_text == text
    assert detail.height >= 4
    assert detail.tabs == (
        InspectorTab.SUMMARY,
        InspectorTab.INPUT,
        InspectorTab.RESULT,
        InspectorTab.TIMING,
    )
