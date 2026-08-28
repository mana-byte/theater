"""Focused logical tool-row coverage for the Régie trajectory ledger."""

from __future__ import annotations

import json
from io import StringIO
from types import MappingProxyType

import pytest
from rich.console import Console
from rich.style import Style
from textual.app import App, ComposeResult
from textual.widgets import LoadingIndicator, RichLog, Tab, TabbedContent

from theater.constants.regie_trajectory import TOOL_ROW_SUMMARY_MAX_CHARS
from theater.constants.trajectory import TRAJECTORY_DETAIL_RECORD_MAX_BYTES
from theater.regie.trajectory.enums import InspectorTab
from theater.regie.trajectory.inspection.links import (
    DETAIL_JSON_TOGGLE_META,
    DETAIL_RECORD_TARGET_META,
)
from theater.regie.trajectory.inspection.rich_content import DetailStyles
from theater.regie.trajectory.inspection.styled import (
    build_tool_span_details,
)
from theater.regie.trajectory.inspection.tools import tool_detail_text
from theater.regie.trajectory.render.pagination import paginate_search_result
from theater.regie.trajectory.render.requests import RequestIndex
from theater.regie.trajectory.render.tools import build_tool_index, tool_row_text
from theater.regie.trajectory.search import search_records
from theater.regie.trajectory.state import TrajectoryStateStore
from theater.regie.trajectory.view import TrajectoryView
from theater.regie.trajectory.widgets.footer import TrajectoryFooter
from theater.regie.trajectory.widgets.ledger import Ledger
from theater.regie.trajectory.widgets.span_detail import SpanDetailPanel
from theater.trajectory import (
    ContentFormat,
    ContentPreview,
    DetailField,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryRequest,
    TrajectoryRequestIdentity,
    TrajectoryStatus,
    bounded_preview,
    group_records,
)
from theater.trajectory.enums import TrajectoryFailureCategory
from theater.trajectory.records import TrajectoryFailure


def _tool(
    record_id: str,
    index: int,
    kind: TrajectoryKind,
    call_id: str | None,
    *,
    summary: str = "tool",
    details: tuple[DetailField, ...] = (),
    participant_id: str = "participant",
    source_epoch: str = "epoch",
    request_id: str | None = None,
    status: TrajectoryStatus = TrajectoryStatus.COMPLETED,
    revision: int = 1,
    turn_id: str | None = None,
    step_id: str | None = None,
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
        source="codex",
        summary=summary,
        status=status,
        raw_index=index,
        call_id=call_id,
        request_id=request_id,
        turn_id=turn_id,
        step_id=step_id,
        details=details,
        failure=failure,
        retry_of_record_id=retry_of_record_id,
        retry_attempt=retry_attempt,
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
    assert build_tool_span_details(operation, InspectorTab.RESULT).tabs == (InspectorTab.SUMMARY,)


def test_tool_summary_exposes_typed_failure_and_retry_target() -> None:
    result = _tool(
        "result",
        2,
        TrajectoryKind.TOOL_RESULT,
        "one",
        status=TrajectoryStatus.ERROR,
        failure=TrajectoryFailure(
            TrajectoryFailureCategory.TOOL,
            code="exit_1",
            detail="command failed",
        ),
        retry_of_record_id="prior",
        retry_attempt=2,
    )
    operation = build_tool_index((result,)).ordered[0]
    detail = build_tool_span_details(operation, InspectorTab.SUMMARY)

    assert "Failure: tool" in detail.copy_text
    assert "Code: exit_1" in detail.copy_text
    assert "Retry of: prior · attempt 2" in detail.copy_text
    assert any(
        getattr(span.style, "meta", {}).get(DETAIL_RECORD_TARGET_META) == "prior"
        for span in detail.content.spans
    )


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


def test_tool_summary_prefers_structured_call_input_over_result_text() -> None:
    call = _tool(
        "call",
        1,
        TrajectoryKind.TOOL_CALL,
        "one",
        details=(
            DetailField.from_text("tool", "runner"),
            DetailField.from_text(
                "arguments",
                '{"noise":"ignored","query":"needle","path":"src/app.py",'
                '"command":"uv run pytest"}',
                format=ContentFormat.JSON,
            ),
        ),
    )
    result = _tool(
        "result",
        2,
        TrajectoryKind.TOOL_RESULT,
        "one",
        details=(DetailField.from_text("result", "verbose output that should stay in details"),),
    )
    operation = build_tool_index((call, result)).ordered[0]

    assert tool_row_text(operation).summary == (
        "[runner] command=uv run pytest · path=src/app.py · query=needle"
    )
    assert tool_row_text(operation, compact=True).summary == "[runner] command=uv run pytest"
    assert "verbose output" not in tool_row_text(operation).summary


def test_tool_details_bound_copy_and_show_omission() -> None:
    preview = ContentPreview(text='{"value":"' + "x" * 5000 + '"}', omitted_bytes=77)
    field = DetailField("result", preview, ContentFormat.JSON)
    result = _tool("result", 2, TrajectoryKind.TOOL_RESULT, "one", details=(field,))
    operation = build_tool_index((result,)).ordered[0]
    text = tool_detail_text(operation, InspectorTab.RESULT)
    detail = build_tool_span_details(operation, InspectorTab.RESULT)

    assert len(text.encode()) <= TRAJECTORY_DETAIL_RECORD_MAX_BYTES
    assert "77 source bytes omitted" in text
    assert detail.copy_text == text
    assert detail.tabs == (
        InspectorTab.SUMMARY,
        InspectorTab.RESULT,
    )

    themed = build_tool_span_details(
        operation,
        InspectorTab.RESULT,
        styles=DetailStyles(
            text=Style(color="#eeeeee", bgcolor="#101010"),
            accent=Style(color="#ffaa00"),
            code=Style(color="#eeeeee", bgcolor="#202020"),
            muted=Style(color="#888888"),
            error=Style(color="#ff0000"),
            success=Style(color="#00ff00"),
        ),
    )
    rendered = Console(width=80, color_system="truecolor").render(themed.content)
    assert any(
        segment.style is not None
        and segment.style.bgcolor is not None
        and segment.style.bgcolor.name == "#202020"
        for segment in rendered
    )


def test_tool_json_details_expand_formatted_string_values() -> None:
    field = DetailField.from_text(
        "result",
        json.dumps(
            {
                "type": "text",
                "text": "## Result\n\n- **Passed** checks\n- Read `src/app.py`",
                "short": "kept inline",
            }
        ),
        format=ContentFormat.JSON,
    )
    result = _tool("result", 2, TrajectoryKind.TOOL_RESULT, "one", details=(field,))
    operation = build_tool_index((result,)).ordered[0]
    detail = build_tool_span_details(operation, InspectorTab.RESULT)
    output = StringIO()
    console = Console(width=80, file=output)

    console.print(detail.content)
    rendered = output.getvalue()

    assert '"text": ▾' in rendered
    assert '"short": "kept inline"' in rendered
    assert "• Passed checks" in rendered
    assert r"## Result\n\n- **Passed** checks" in detail.copy_text

    toggle_key = next(
        segment.style.meta[DETAIL_JSON_TOGGLE_META]
        for segment in Console(width=80).render(detail.content)
        if segment.style is not None and DETAIL_JSON_TOGGLE_META in segment.style.meta
    )
    collapsed = build_tool_span_details(
        operation,
        InspectorTab.RESULT,
        collapsed_json_paths=frozenset({toggle_key}),
    )
    collapsed_output = StringIO()
    collapsed_console = Console(width=80, file=collapsed_output)
    collapsed_console.print(collapsed.content)
    collapsed_text = collapsed_output.getvalue()
    assert '"text": ▸' in collapsed_text
    assert "• Passed checks" not in collapsed_text

    bounded = bounded_preview("y" * 5000, max_bytes=128)
    bounded_result = _tool(
        "bounded-result",
        3,
        TrajectoryKind.TOOL_RESULT,
        "bounded",
        details=(DetailField("result", bounded, ContentFormat.TEXT),),
    )
    bounded_text = tool_detail_text(
        build_tool_index((bounded_result,)).ordered[0], InspectorTab.RESULT
    )
    marker = f"… {bounded.omitted_bytes} bytes omitted …"
    assert bounded_text.count(marker) == 1


@pytest.mark.asyncio
async def test_json_string_blocks_toggle_from_the_detail_log() -> None:
    item = _tool(
        "call",
        1,
        TrajectoryKind.TOOL_CALL,
        "one",
        details=(
            DetailField.from_text(
                "arguments",
                json.dumps({"text": "first line\nsecond line"}),
                format=ContentFormat.JSON,
            ),
        ),
    )

    class DetailHost(App):
        def compose(self) -> ComposeResult:
            yield SpanDetailPanel()

    app = DetailHost()
    async with app.run_test(size=(80, 24)) as pilot:
        panel = app.query_one(SpanDetailPanel)
        panel.set_span(item, tab=InspectorTab.INPUT)
        await pilot.pause()
        log = panel.query_one(f"#{panel._log_id(panel.tab)}", RichLog)
        target: tuple[int, int] | None = None
        toggle_key: str | None = None
        for row, strip in enumerate(log.lines):
            column = 0
            for segment in strip:
                meta = segment.style.meta if segment.style is not None else {}
                if isinstance(meta.get(DETAIL_JSON_TOGGLE_META), str):
                    target = (column + 2, row + 1)
                    toggle_key = meta[DETAIL_JSON_TOGGLE_META]
                    break
                column += segment.cell_length
            if target is not None:
                break

        assert target is not None and toggle_key is not None
        await pilot.click(log, offset=target)
        await pilot.pause()

        assert toggle_key in panel._collapsed_json_paths
        assert log.styles.background_tint.a == 0
        assert panel._details is not None
        output = StringIO()
        console = Console(width=80, file=output)
        console.print(panel._details.content)
        assert '"text": ▸' in output.getvalue()


@pytest.mark.asyncio
async def test_span_detail_defers_one_render_behind_loading_indicator(monkeypatch) -> None:
    item = _tool(
        "call",
        1,
        TrajectoryKind.TOOL_CALL,
        "one",
        details=(DetailField.from_text("arguments", '{"path":"src"}'),),
    )

    class DetailHost(App):
        def compose(self) -> ComposeResult:
            yield SpanDetailPanel()

    app = DetailHost()
    async with app.run_test(size=(80, 24)) as pilot:
        panel = app.query_one(SpanDetailPanel)
        callbacks = []
        writes = 0
        original_write = RichLog.write

        def defer(callback, *args, **kwargs) -> bool:
            callbacks.append(lambda: callback(*args, **kwargs))
            return True

        def count_write(log, *args, **kwargs):
            nonlocal writes
            writes += 1
            return original_write(log, *args, **kwargs)

        monkeypatch.setattr(panel, "call_after_refresh", defer)
        monkeypatch.setattr(RichLog, "write", count_write)

        panel.set_span(item, tab=InspectorTab.INPUT)
        indicator = panel.query_one("#trajectory-span-detail-loading", LoadingIndicator)
        assert indicator.display
        assert writes == 0

        await pilot.pause()
        callbacks.pop(0)()

        assert not indicator.display
        assert writes == 1
        assert panel.query_one(f"#{panel._log_id(InspectorTab.INPUT)}", RichLog).lines


def _request(
    request_id: str,
    source_request_id: str,
    *,
    participant_id: str = "participant",
    source_epoch: str = "epoch",
) -> TrajectoryRequest:
    return TrajectoryRequest(
        request_id=request_id,
        participant_id=participant_id,
        source_epoch=source_epoch,
        source="codex",
        record_ids=(f"member-{request_id}",),
        identity=TrajectoryRequestIdentity.SOURCE,
        source_request_id=source_request_id,
    )


def _request_index(
    *requests: TrajectoryRequest,
    direct: dict[str, str] | None = None,
) -> RequestIndex:
    return RequestIndex(
        ordered=requests,
        by_id=MappingProxyType({request.request_id: request for request in requests}),
        by_record_id=MappingProxyType(direct or {}),
    )


def test_tool_request_fallback_requires_one_exact_unconflicted_match() -> None:
    call = _tool(
        "call",
        1,
        TrajectoryKind.TOOL_CALL,
        "tool-call",
        request_id="source-request",
    )
    tool_index = build_tool_index((call,))
    exact = _request("exact", "source-request")

    result = search_records((call,), request_index=_request_index(exact), tool_index=tool_index)
    assert result.request_id_by_row_id == {"call": "exact"}
    assert sum(entry.is_request_header for entry in result.entries) == 1

    for mismatch in (
        _request("participant-mismatch", "source-request", participant_id="other"),
        _request("epoch-mismatch", "source-request", source_epoch="other"),
    ):
        result = search_records(
            (call,), request_index=_request_index(mismatch), tool_index=tool_index
        )
        assert result.request_id_by_row_id == {}
        assert not any(entry.is_request_header for entry in result.entries)

    duplicate = _request("duplicate", "source-request")
    ambiguous = search_records(
        (call,), request_index=_request_index(exact, duplicate), tool_index=tool_index
    )
    assert ambiguous.request_id_by_row_id == {}

    direct = _request("direct", "different-source-request")
    conflicted = search_records(
        (call,),
        request_index=_request_index(exact, direct, direct={"call": "direct"}),
        tool_index=tool_index,
    )
    assert conflicted.request_id_by_row_id == {}
    assert not any(entry.is_request_header for entry in conflicted.entries)


def test_result_match_keeps_the_logical_anchor_group_path() -> None:
    call = _tool(
        "call",
        1,
        TrajectoryKind.TOOL_CALL,
        "one",
        summary="invoke",
        turn_id="call-turn",
        step_id="call-step",
    )
    result_record = _tool(
        "result",
        2,
        TrajectoryKind.TOOL_RESULT,
        "one",
        summary="needle result",
        turn_id="result-turn",
        step_id="result-step",
    )
    records = (call, result_record)
    result = search_records(
        records,
        query="needle",
        groups=group_records(records),
        tool_index=build_tool_index(records),
    )

    assert result.row_ids == ("call",)
    assert result.path_for_record("call")
    assert result.path_for_record("call") != result.path_for_record("result")
    page = paginate_search_result(result, 0, 1)
    assert page.result.group_paths == {"call": result.path_for_record("call")}


class _LedgerHost(App):
    def compose(self) -> ComposeResult:
        yield TrajectoryView("participant")


@pytest.mark.asyncio
async def test_tool_detail_tabs_render_and_move_in_contextual_order() -> None:
    call = _tool(
        "call",
        1,
        TrajectoryKind.TOOL_CALL,
        "one",
        details=(DetailField.from_text("arguments", '{"path":"src"}'),),
    )
    result = _tool(
        "result",
        2,
        TrajectoryKind.TOOL_RESULT,
        "one",
        details=(DetailField.from_text("result", "done"),),
    )
    app = _LedgerHost()

    async with app.run_test(size=(110, 24)) as pilot:
        view = app.query_one(TrajectoryView)
        view.state.upsert((call, result))
        view._refresh()
        view._open_details("call")
        await pilot.pause()

        panel = view.query_one(SpanDetailPanel)
        content = panel.query_one(TabbedContent)
        visible_tabs = [tab for tab in content.query(Tab) if tab.display]
        expected_tabs = [content.get_tab(panel._pane_id(tab)) for tab in panel.tabs]
        assert visible_tabs == expected_tabs
        log = panel.query_one(f"#{panel._log_id(panel.tab)}", RichLog)
        assert all(line.cell_length == log.scrollable_content_region.width for line in log.lines)

        for expected in panel.tabs[1:]:
            await pilot.press("l")
            assert panel.tab is expected


@pytest.mark.asyncio
async def test_span_detail_reflows_to_the_available_width() -> None:
    item = _tool(
        "call",
        1,
        TrajectoryKind.TOOL_CALL,
        "one",
        details=(
            DetailField.from_text(
                "arguments",
                "A long detail value that should wrap against the current viewport width. " * 8,
            ),
        ),
    )
    app = _LedgerHost()

    async with app.run_test(size=(52, 24)) as pilot:
        view = app.query_one(TrajectoryView)
        view.state.upsert((item,))
        view._refresh()
        view.state.detail_tab = InspectorTab.INPUT
        view._open_details("call")
        await pilot.pause()

        panel = view.query_one(SpanDetailPanel)
        log = panel.query_one(f"#{panel._log_id(panel.tab)}", RichLog)
        narrow_width = log.scrollable_content_region.width
        assert log.virtual_size.width == narrow_width

        await pilot.resize_terminal(100, 24)
        await pilot.pause()

        wide_width = log.scrollable_content_region.width
        assert wide_width > narrow_width
        assert log.virtual_size.width == wide_width

        for _ in range(5):
            for key in ("h", "l"):
                await pilot.press(key)
                await pilot.pause()
                log = panel.query_one(f"#{panel._log_id(panel.tab)}", RichLog)
                assert log.scrollable_content_region.width > 1
                assert log.virtual_size.width == log.scrollable_content_region.width


@pytest.mark.asyncio
async def test_result_member_hover_and_expansion_target_combined_row() -> None:
    call = _tool(
        "call",
        1,
        TrajectoryKind.TOOL_CALL,
        "one",
        details=(
            DetailField.from_text("tool", "runner"),
            DetailField.from_text("args", '{"path":"src"}'),
        ),
        status=TrajectoryStatus.RUNNING,
    )
    result_record = _tool(
        "result",
        2,
        TrajectoryKind.TOOL_RESULT,
        "one",
        details=(DetailField.from_text("result", "finished"),),
    )
    records = (call, result_record)
    search = search_records(records, tool_index=build_tool_index(records))
    operation = next(iter(search.tools.values()))

    app = _LedgerHost()
    async with app.run_test(size=(110, 24)) as pilot:
        view = app.query_one(TrajectoryView)
        view.state.upsert(records)
        view.state.follow_tail = False
        view.state.select("result")
        view._refresh()
        ledger = view.query_one(Ledger)
        await pilot.pause()
        row_key = f"{Ledger.TOOL_PREFIX}{operation.operation_id}"

        ledger.set_hovered("result")
        position = ledger.get_cell(row_key, Ledger.COLUMN_POSITION)
        summary = ledger.get_cell(row_key, Ledger.COLUMN_SUMMARY)
        assert "●" in position.plain
        assert summary.get_style_at_offset(Console(), 1).bold
        assert "COMPLETED" not in summary.plain

        view.state.detail_tab = InspectorTab.RESULT
        view._open_details("result")
        await pilot.pause()
        panel = view.query_one(SpanDetailPanel)
        assert view.state.detail_id == "call"
        assert "finished" in panel.copy_text
        assert ledger.has_class("-hidden")


@pytest.mark.asyncio
async def test_call_only_to_matched_patches_without_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = _tool(
        "call",
        1,
        TrajectoryKind.TOOL_CALL,
        "one",
        details=(DetailField.from_text("tool", "runner"),),
        status=TrajectoryStatus.RUNNING,
    )
    app = _LedgerHost()

    async with app.run_test(size=(110, 24)) as pilot:
        view = app.query_one(TrajectoryView)
        view.state.upsert((call,))
        view.state.follow_tail = False
        view.state.select("call")
        view.state.detail_tab = InspectorTab.RESULT
        view._refresh()
        view._open_details("call")
        await pilot.pause()
        ledger = view.query_one(Ledger)
        panel = view.query_one(SpanDetailPanel)
        operation_id = next(iter(view.state.tool_index.by_id))
        row_key = f"{Ledger.TOOL_PREFIX}{operation_id}"
        assert panel.tab is InspectorTab.SUMMARY
        assert InspectorTab.RESULT not in panel.tabs

        rebuilds = 0
        original_rebuild = ledger._rebuild

        def count_rebuild(*, preserve_scroll: bool = True) -> None:
            nonlocal rebuilds
            rebuilds += 1
            original_rebuild(preserve_scroll=preserve_scroll)

        monkeypatch.setattr(ledger, "_rebuild", count_rebuild)
        result_record = _tool(
            "result",
            2,
            TrajectoryKind.TOOL_RESULT,
            "one",
            details=(DetailField.from_text("result", "finished"),),
        )
        records = (call, result_record)
        view.state.upsert((result_record,))
        view._refresh()
        matched = search_records(records, tool_index=build_tool_index(records))

        assert next(iter(matched.tools)) == operation_id
        assert matched.row_ids == ("call",)
        assert ledger.get_row_index(row_key) >= 0
        assert rebuilds == 0
        assert InspectorTab.RESULT in panel.tabs
        panel.set_tab(InspectorTab.RESULT)
        assert "finished" in panel.copy_text

        view._refresh()
        assert rebuilds == 0


class _ViewHost(App):
    def __init__(self, state_store: TrajectoryStateStore) -> None:
        super().__init__()
        self.state_store = state_store

    def compose(self) -> ComposeResult:
        yield TrajectoryView("participant", state_store=self.state_store)


@pytest.mark.asyncio
async def test_result_selection_reveals_anchor_page_and_footer_counts_logical_rows() -> None:
    first = _ordinary("first", 1)
    call = _tool("call", 2, TrajectoryKind.TOOL_CALL, "one", summary="invoke")
    result_record = _tool("result", 3, TrajectoryKind.TOOL_RESULT, "one", summary="needle result")
    app = _ViewHost(TrajectoryStateStore(page_size=1))

    async with app.run_test(size=(110, 30)) as pilot:
        view = app.query_one(TrajectoryView)
        view.state.upsert((first, call, result_record))
        view.state.follow_tail = False
        view.state.select("first")
        view._refresh()
        await pilot.pause()

        footer = view.query_one(TrajectoryFooter)
        assert "2 loaded" in str(footer.query_one("#trajectory-status").content)
        assert "1–1/2 items" in str(footer.query_one("#trajectory-page-range").content)
        assert view._reveal_selection_page("result")
        assert view.state.ledger_page == 1
        view._update_follow_for_selection("result")
        assert view.state.follow_tail

        view.state.query = "needle"
        view._refresh()
        await pilot.pause()
        assert view.search_result.row_ids == ("call",)
        assert "2 loaded" in str(footer.query_one("#trajectory-status").content)
        assert "1–1/1 items" in str(footer.query_one("#trajectory-page-range").content)
