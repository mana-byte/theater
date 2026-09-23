"""Focused logical tool-row coverage for the Régie trajectory ledger."""

from __future__ import annotations

import json
from io import StringIO
from types import MappingProxyType

import pytest
from regie.trajectory.domain import (
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
)
from regie.trajectory.domain.enums import TrajectoryFailureCategory
from regie.trajectory.domain.records import TrajectoryFailure
from regie.trajectory.limits import TRAJECTORY_DETAIL_RECORD_MAX_BYTES
from regie.trajectory.rich.enums import InspectorTab
from regie.trajectory.rich.inspection.links import (
    DETAIL_JSON_TOGGLE_META,
    DETAIL_RECORD_TARGET_META,
)
from regie.trajectory.rich.inspection.rich_content import DetailStyles
from regie.trajectory.rich.inspection.styled import (
    build_tool_span_details,
)
from regie.trajectory.rich.inspection.tools import tool_detail_text
from regie.trajectory.rich.render.requests import RequestIndex
from regie.trajectory.rich.render.tools import build_tool_index, tool_row_text
from regie.trajectory.rich.state import TrajectoryStateStore
from regie.trajectory.rich.view import TrajectoryView
from regie.trajectory.rich.widgets.span_detail import SpanDetailPanel
from regie.trajectory.ui_constants import TOOL_ROW_SUMMARY_MAX_CHARS
from rich.console import Console
from rich.style import Style
from textual.app import App, ComposeResult
from textual.geometry import Region
from textual.widgets import LoadingIndicator, RichLog


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
    mcp_server: str | None = None,
    mcp_tool: str | None = None,
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
        mcp_server=mcp_server,
        mcp_tool=mcp_tool,
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


def test_mcp_tool_row_and_summary_expose_protocol_identity() -> None:
    call = _tool(
        "call",
        1,
        TrajectoryKind.TOOL_CALL,
        "one",
        details=(DetailField.from_text("tool", "grafana_query"),),
        mcp_server="grafana",
        mcp_tool="query",
    )
    operation = build_tool_index((call,)).ordered[0]

    row = tool_row_text(operation)
    assert row.event == "◇ MCP"
    assert row.summary.startswith("[grafana/query]")
    summary = tool_detail_text(operation, InspectorTab.SUMMARY)
    assert "MCP server: grafana" in summary
    assert "MCP tool: query" in summary


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


@pytest.mark.asyncio
async def test_span_detail_drops_reflow_when_callback_cannot_be_scheduled(monkeypatch) -> None:
    item = _tool("call", 1, TrajectoryKind.TOOL_CALL, "one")

    class DetailHost(App):
        def compose(self) -> ComposeResult:
            yield SpanDetailPanel()

    app = DetailHost()
    async with app.run_test(size=(80, 24)) as pilot:
        panel = app.query_one(SpanDetailPanel)
        panel.set_span(item)
        await pilot.pause()

        monkeypatch.setattr(
            RichLog,
            "scrollable_content_region",
            property(lambda _log: Region(0, 0, 0, 0)),
        )
        monkeypatch.setattr(panel, "call_after_refresh", lambda *_args, **_kwargs: False)

        panel._schedule_reflow(force=True, loading=True)

        assert not panel._reflow_pending
        assert not panel._reflow_force
        assert panel._reflow_scroll_y is None
        assert not panel.query_one("#trajectory-span-detail-loading", LoadingIndicator).display


@pytest.mark.asyncio
async def test_span_detail_waits_for_later_reflow_when_width_is_zero(monkeypatch) -> None:
    item = _tool("call", 1, TrajectoryKind.TOOL_CALL, "one")

    class DetailHost(App):
        def compose(self) -> ComposeResult:
            yield SpanDetailPanel()

    app = DetailHost()
    async with app.run_test(size=(80, 24)) as pilot:
        panel = app.query_one(SpanDetailPanel)
        panel.set_span(item)
        await pilot.pause()
        log = panel.query_one(f"#{panel._log_id(panel.tab)}", RichLog)
        original_region = RichLog.scrollable_content_region
        width_is_zero = True
        callbacks = []

        def content_region(widget):
            if widget is log and width_is_zero:
                return Region(0, 0, 0, 0)
            return original_region.__get__(widget, RichLog)

        def defer(callback, *args, **kwargs) -> bool:
            callbacks.append(lambda: callback(*args, **kwargs))
            return True

        monkeypatch.setattr(RichLog, "scrollable_content_region", property(content_region))
        monkeypatch.setattr(panel, "call_after_refresh", defer)

        panel._schedule_reflow(force=True)
        callbacks.pop(0)()

        assert not callbacks
        assert not panel._reflow_pending
        assert panel._reflow_force

        width_is_zero = False
        panel._schedule_reflow()
        callbacks.pop(0)()

        assert not panel._reflow_force
        assert panel._rendered_widths[panel.tab] == log.scrollable_content_region.width


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


class _LedgerHost(App):
    def compose(self) -> ComposeResult:
        yield TrajectoryView("participant")


class _ViewHost(App):
    def __init__(self, state_store: TrajectoryStateStore) -> None:
        super().__init__()
        self.state_store = state_store

    def compose(self) -> ComposeResult:
        yield TrajectoryView("participant", state_store=self.state_store)
