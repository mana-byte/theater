from __future__ import annotations

from dataclasses import replace

import pytest
from rich.console import Console
from textual.app import App, ComposeResult
from textual.coordinate import Coordinate

import theater.regie.trajectory.state as state_module
from theater.regie.trajectory.enums import InspectorTab
from theater.regie.trajectory.inspection.lines import (
    request_association_lines,
    request_timing_lines,
    request_usage_lines,
)
from theater.regie.trajectory.inspection.styled import build_span_details
from theater.regie.trajectory.render.pagination import paginate_search_result
from theater.regie.trajectory.render.requests import build_request_index, request_row_text
from theater.regie.trajectory.search import search_records
from theater.regie.trajectory.state import ParticipantTrajectoryState
from theater.regie.trajectory.view import TrajectoryView
from theater.regie.trajectory.widgets.ledger import (
    Ledger,
    LedgerRecordClicked,
    LedgerRecordHovered,
)
from theater.regie.trajectory.widgets.span_detail import SpanDetailRecordLinkClicked
from theater.regie.trajectory.widgets.timeline import Timeline
from theater.trajectory import (
    PanelState,
    PanelStateInfo,
    Timing,
    TimingProvenance,
    TrajectoryDelta,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryPage,
    TrajectoryRecord,
    TrajectoryStatus,
    TrajectoryUpsert,
    TrajectoryUsage,
    group_records,
)
from theater.trajectory.enums import CostProvenance, TrajectoryFailureCategory
from theater.trajectory.records import TrajectoryFailure


def record(
    record_id: str,
    index: int,
    *,
    request_id: str | None = None,
    usage: TrajectoryUsage | None = None,
    status: TrajectoryStatus = TrajectoryStatus.COMPLETED,
    revision: int = 1,
    turn_id: str | None = None,
    step_id: str | None = None,
    timing: Timing | None = None,
) -> TrajectoryRecord:
    return TrajectoryRecord(
        record_id=record_id,
        revision=revision,
        participant_id="p1",
        source_epoch="epoch",
        lane=TrajectoryLane.MODEL,
        kind=TrajectoryKind.ASSISTANT,
        source="adapter",
        summary=record_id,
        status=status,
        raw_index=index,
        request_id=request_id,
        usage=usage,
        turn_id=turn_id,
        step_id=step_id,
        timing=timing,
    )


def test_request_index_is_immutable_and_state_keeps_the_final_retained_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = record("first", 1, request_id="shared")
    second = record("second", 2, request_id="shared")
    usage = record("usage", 3, usage=TrajectoryUsage(request_id="usage-request"))
    index = build_request_index((first, second, usage))

    shared = index.by_record_id["first"]
    assert index.by_record_id == {
        "first": shared,
        "second": shared,
        "usage": index.by_record_id["usage"],
    }
    assert index.by_id[shared].record_ids == ("first", "second")
    assert index.by_id[index.by_record_id["usage"]].source_request_id == "usage-request"

    monkeypatch.setattr(state_module, "TRAJECTORY_UI_RECORD_LIMIT", 1)
    monkeypatch.setattr(state_module, "TRAJECTORY_UI_MAX_BYTES", 1_000_000)
    state = ParticipantTrajectoryState("p1")
    state.upsert((first, second))
    assert tuple(state.records) == ("second",)
    assert state.request_index.by_record_id == {"second": shared}

    state.apply_snapshot(
        TrajectoryPage(
            PanelStateInfo(PanelState.READY),
            stream_id="stream",
            records=(first,),
        )
    )
    updated = replace(first, revision=2, status=TrajectoryStatus.RUNNING)
    state.apply_follow(TrajectoryDelta("stream", upserts=(TrajectoryUpsert(updated),)))
    prior_index = state.request_index
    assert prior_index.by_id[shared].status is TrajectoryStatus.RUNNING

    state.apply_snapshot(TrajectoryPage(PanelStateInfo(PanelState.STALE), stream_id="stream"))
    assert state.request_index is prior_index
    assert tuple(state.records) == ("first",)


def test_request_headers_follow_matched_records_without_distorting_steps() -> None:
    first = record("first", 1, request_id="shared", turn_id="turn", step_id="step")
    second = record("second", 2, request_id="shared", turn_id="turn", step_id="step")
    index = build_request_index((first, second))
    result = search_records(
        (first, second), groups=group_records((first, second)), request_index=index
    )

    assert [entry.is_request_header for entry in result.entries] == [True, False, False, False]
    assert [entry.depth for entry in result.entries] == [0, 1, 2, 2]
    assert result.entries[1].is_group_header
    assert result.entries[2].record_id == "first"

    plain = search_records((first, second), groups=group_records((first, second)))
    assert not any(entry.is_request_header for entry in plain.entries)
    assert [(entry.is_header, entry.record_id, entry.depth) for entry in plain.entries] == [
        (True, None, 0),
        (False, "first", 1),
        (False, "second", 1),
    ]

    unassociated = record("third", 3, turn_id="turn", step_id="step")
    mixed = search_records(
        (first, unassociated),
        groups=group_records((first, unassociated)),
        request_index=build_request_index((first, unassociated)),
    )
    assert [entry.depth for entry in mixed.entries] == [0, 1, 2, 1]
    assert mixed.entries[0].is_group_header
    assert mixed.entries[1].is_request_header

    searchable = replace(first, usage=TrajectoryUsage(model="model-x", request_id="shared"))
    search_index = build_request_index((searchable, second))
    by_request = search_records((searchable, second), query="shared", request_index=search_index)
    by_model = search_records((searchable, second), query="modelx", request_index=search_index)
    assert by_request.record_ids == ("first", "second")
    assert by_model.record_ids == ("first",)
    assert len(by_model.requests) == 1


def test_request_headers_repeat_per_record_page_without_affecting_ranges() -> None:
    records = tuple(
        record(f"r{index}", index, request_id="shared", turn_id="turn", step_id="step")
        for index in range(3)
    )
    result = search_records(
        records,
        groups=group_records(records),
        request_index=build_request_index(records),
    )

    first = paginate_search_result(result, 0, 1)
    last = paginate_search_result(result, 2, 1)

    assert (first.total_items, first.first_item, first.last_item, first.count) == (3, 1, 1, 3)
    assert (last.total_items, last.first_item, last.last_item, last.count) == (3, 3, 3, 3)
    assert sum(entry.is_request_header for entry in first.result.entries) == 1
    assert sum(entry.is_request_header for entry in last.result.entries) == 1
    assert set(first.result.requests) == set(last.result.requests) == set(result.requests)
    assert [
        (entry.is_request_header, entry.is_group_header, entry.record_id)
        for entry in last.result.entries
    ] == [
        (True, False, None),
        (False, True, None),
        (False, False, "r2"),
    ]


def test_pagination_recomposes_interleaved_request_headers_per_page() -> None:
    records = (
        record("a1", 1, request_id="A"),
        record("b1", 2, request_id="B"),
        record("a2", 3, request_id="A"),
        record("b2", 4, request_id="B"),
    )
    index = build_request_index(records)
    result = search_records(records, request_index=index)
    page = paginate_search_result(result, 1, 2)

    request_a = index.by_record_id["a1"]
    request_b = index.by_record_id["b1"]
    assert [(entry.request_id, entry.record_id) for entry in page.result.entries] == [
        (request_a, None),
        (None, "a2"),
        (request_b, None),
        (None, "b2"),
    ]


def test_pagination_keeps_an_unassociated_record_before_a_later_request_member() -> None:
    records = (
        record("a1", 1, request_id="A"),
        record("filler", 2),
        record("unassociated", 3),
        record("a2", 4, request_id="A"),
    )
    index = build_request_index(records)
    result = search_records(records, request_index=index)
    page = paginate_search_result(result, 1, 2)

    request_a = index.by_record_id["a1"]
    assert [(entry.request_id, entry.record_id) for entry in page.result.entries] == [
        (None, "unassociated"),
        (request_a, None),
        (None, "a2"),
    ]


def test_request_text_marks_missing_values_and_uses_reported_usage() -> None:
    complete = record(
        "complete",
        1,
        request_id="request",
        status=TrajectoryStatus.COMPLETED,
        usage=TrajectoryUsage(
            model="model-x",
            input_tokens=1_200,
            output_tokens=34,
            cache_read_tokens=5,
            cache_write_tokens=6,
            reasoning_tokens=7,
            cost_usd=0.1234,
            cost_provenance=CostProvenance.REPORTED,
        ),
        timing=Timing(duration_ms=2_000, provenance=TimingProvenance.SOURCE),
    )
    request = build_request_index((complete,)).ordered[0]
    text = request_row_text(request, compact=True)

    assert text.event == "◆ REQUEST"
    assert text.source == "model-x"
    assert text.summary == (
        "[model-x] in 1.2K · out 34 · cache 11 · reasoning 7 · cost $0.1234 reported"
    )
    assert (text.status, text.duration) == ("completed", "2.0s")

    missing_request = build_request_index((record("missing", 2, request_id="missing"),)).ordered[0]
    missing = request_row_text(missing_request)
    assert (missing.source, missing.summary) == ("model unknown", "usage unavailable")


def test_request_inspector_exposes_diagnostics_and_exact_associations() -> None:
    context = replace(
        record("context", 1, request_id="request"),
        kind=TrajectoryKind.CONTEXT,
    )
    model = replace(
        record(
            "model",
            2,
            request_id="request",
            usage=TrajectoryUsage(
                model="model-x",
                provider="provider-x",
                output_tokens=100,
                cost_usd=0.25,
                cost_provenance=CostProvenance.REPORTED,
            ),
            timing=Timing(
                start=10.0,
                first_token=10.2,
                end=11.2,
                provenance=TimingProvenance.SOURCE,
            ),
        ),
        status=TrajectoryStatus.ERROR,
        failure=TrajectoryFailure(
            TrajectoryFailureCategory.PROVIDER,
            code="rate_limit",
            detail="retry later",
        ),
        retry_of_record_id="prior",
        retry_attempt=2,
    )
    tool = replace(
        record("tool", 3, request_id="request"),
        lane=TrajectoryLane.TOOLS,
        kind=TrajectoryKind.TOOL_CALL,
    )
    coordination = replace(
        record("coordination", 4, request_id="request"),
        lane=TrajectoryLane.THEATER,
        kind=TrajectoryKind.SEND,
    )
    request = build_request_index((context, model, tool, coordination)).ordered[0]

    usage = "\n".join(line.text for line in request_usage_lines(request))
    timing = "\n".join(line.text for line in request_timing_lines(request))
    associations = request_association_lines(request)
    details = build_span_details(
        model,
        InspectorTab.ASSOCIATIONS,
        request=request,
    )

    assert "Provider: provider-x" in usage
    assert request_row_text(request).source == "provider-x/model-x"
    assert "Cost: $0.25 · reported" in usage
    assert "Time to first token: 200ms" in timing
    assert "Generation duration: 1s" in timing
    assert "Output throughput: 100.00 tok/s" in timing
    assert {line.target_record_id for line in associations if line.target_record_id} == {
        "context",
        "model",
        "tool",
        "coordination",
        "prior",
    }
    assert "Retry of: prior · attempt 2" in details.copy_text
    assert InspectorTab.ASSOCIATIONS in details.tabs


class LedgerHost(App):
    def __init__(self) -> None:
        super().__init__()
        self.clicked: list[str | None] = []
        self.hovered: list[str | None] = []

    def compose(self) -> ComposeResult:
        yield Ledger()

    def on_ledger_record_clicked(self, message: LedgerRecordClicked) -> None:
        self.clicked.append(message.record_id)

    def on_ledger_record_hovered(self, message: LedgerRecordHovered) -> None:
        self.hovered.append(message.record_id)


@pytest.mark.asyncio
async def test_ledger_request_headers_are_noninteractive_and_patch_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = record(
        "record",
        1,
        request_id="request",
        status=TrajectoryStatus.PENDING,
        usage=TrajectoryUsage(model="model-x", input_tokens=1, cost_usd=0.1),
    )
    app = LedgerHost()
    async with app.run_test(size=(100, 20)) as pilot:
        ledger = app.query_one(Ledger)
        first = search_records((initial,), request_index=build_request_index((initial,)))
        ledger.update_rows((initial,), first, selected_id="record")
        request_id = next(iter(first.requests))
        key = f"{Ledger.REQUEST_PREFIX}{request_id}"
        row = ledger.get_row_index(key)
        assert ledger.ordered_rows[row].height == 2
        assert ledger.line_ids == (None, "record")
        assert "model-x" in ledger.get_cell(key, Ledger.COLUMN_SOURCE).plain
        assert ledger.get_cell(key, Ledger.COLUMN_POSITION).plain.strip() == "↗"
        assert "cost $0.1" in ledger.get_cell(key, Ledger.COLUMN_SUMMARY).plain
        request_style = ledger._component("request")
        assert request_style.bgcolor is None
        cells = {
            column: ledger.get_cell(key, column)
            for column in (
                Ledger.COLUMN_POSITION,
                Ledger.COLUMN_EVENT,
                Ledger.COLUMN_SOURCE,
                Ledger.COLUMN_SUMMARY,
                Ledger.COLUMN_STATUS,
                Ledger.COLUMN_DURATION,
            )
        }
        assert all(cell.plain.startswith("\n") for cell in cells.values())
        styles = {column: cell.get_style_at_offset(Console(), 1) for column, cell in cells.items()}
        assert all(style.bgcolor == request_style.bgcolor for style in styles.values())
        assert styles[Ledger.COLUMN_EVENT].bold
        assert styles[Ledger.COLUMN_SOURCE].dim
        assert styles[Ledger.COLUMN_SUMMARY].dim
        assert styles[Ledger.COLUMN_DURATION].dim
        assert styles[Ledger.COLUMN_STATUS].color == ledger._status_style(initial.status).color
        assert cells[Ledger.COLUMN_STATUS].get_style_at_offset(Console(), 3).dim

        await pilot.click(ledger, offset=(2, ledger.header_height + row * 2 + 1))
        ledger.focus()
        ledger.move_cursor(row=row, column=0, animate=False)
        await pilot.press("enter")
        ledger._show_hover_cursor = True
        ledger.watch_hover_coordinate(Coordinate(-1, -1), Coordinate(row, 0))
        await pilot.pause()
        assert ledger._selected_id == "record"
        assert app.clicked == []
        assert app.hovered[-1] is None
        assert ledger.rendered_record_count == 1

        updated = replace(
            initial,
            revision=2,
            status=TrajectoryStatus.RUNNING,
            usage=TrajectoryUsage(model="model-x", input_tokens=99, cost_usd=0.2),
        )
        rebuilds = 0
        original_rebuild = ledger._rebuild

        def count_rebuild(*, preserve_scroll: bool = True) -> None:
            nonlocal rebuilds
            rebuilds += 1
            original_rebuild(preserve_scroll=preserve_scroll)

        monkeypatch.setattr(ledger, "_rebuild", count_rebuild)
        second = search_records((updated,), request_index=build_request_index((updated,)))
        ledger.update_rows((updated,), second, selected_id="record")
        assert rebuilds == 0
        assert "in 99" in ledger.get_cell(key, Ledger.COLUMN_SUMMARY).plain
        assert "running" in ledger.get_cell(key, Ledger.COLUMN_STATUS).plain


@pytest.mark.asyncio
async def test_view_places_request_before_step_and_keeps_record_navigation() -> None:
    records = (
        record("first", 1, request_id="shared", turn_id="turn", step_id="step"),
        record("second", 2, request_id="shared", turn_id="turn", step_id="step"),
        record("third", 3, request_id="other", turn_id="next", step_id="next-step"),
    )

    class ViewHost(App):
        def compose(self) -> ComposeResult:
            yield TrajectoryView("p1")

    app = ViewHost()
    async with app.run_test(size=(100, 30)) as pilot:
        view = app.query_one(TrajectoryView)
        view.state_store.page_size = 1
        view.state.upsert(records)
        view._refresh()
        ledger = app.query_one(Ledger)
        assert ledger.entries[0].is_request_header
        assert ledger.entries[1].is_group_header
        group_key = f"{Ledger.GROUP_PREFIX}{ledger.entries[1].group_id}"
        assert ledger.ordered_rows[ledger.get_row_index(group_key)].height == 2
        assert view._selected_visible_ids() == ("third",)
        await pilot.press("shift+h")
        assert view._selected_visible_ids() == ("second",)
        await pilot.press("k")
        assert view.state.selected_id == "second"


@pytest.mark.asyncio
async def test_accounting_records_feed_requests_without_rendering_as_activity() -> None:
    answer = record("answer", 1, request_id="request")
    accounting = replace(
        record(
            "accounting",
            2,
            request_id="request",
            usage=TrajectoryUsage(model="model-x", input_tokens=42, output_tokens=7),
        ),
        kind=TrajectoryKind.USAGE,
        summary="",
    )

    class ViewHost(App):
        def compose(self) -> ComposeResult:
            yield TrajectoryView("p1")

    app = ViewHost()
    async with app.run_test(size=(100, 30)):
        view = app.query_one(TrajectoryView)
        view.state.upsert((answer, accounting))
        view._refresh()
        ledger = view.query_one(Ledger)

        assert tuple(view.state.records) == ("answer", "accounting")
        assert [item.record_id for item in view.state.display_records] == ["answer"]
        assert view.state.selected_id == "answer"
        request = view.state.request_index.ordered[0]
        assert request.usage == accounting.usage
        assert view.query_one(Timeline).span_ids == ("answer",)
        assert ledger.line_ids == (None, "answer")
        assert all(entry.record_id != "accounting" for entry in ledger.entries)


def test_accounting_follow_update_does_not_announce_new_activity() -> None:
    answer = record("answer", 1, request_id="request")
    accounting = replace(
        record(
            "accounting",
            2,
            request_id="request",
            usage=TrajectoryUsage(input_tokens=42),
        ),
        kind=TrajectoryKind.USAGE,
        summary="",
    )
    state = ParticipantTrajectoryState("p1")
    state.apply_snapshot(
        TrajectoryPage(
            PanelStateInfo(PanelState.READY),
            stream_id="stream",
            records=(answer,),
        )
    )
    state.pause_follow()

    assert state.apply_follow(
        TrajectoryDelta("stream", upserts=(TrajectoryUpsert(accounting),))
    ) == (1, 0)
    assert state.new_count == 0
    assert state.selected_id == "answer"
    assert state.request_index.ordered[0].usage == accounting.usage


@pytest.mark.asyncio
async def test_loaded_request_association_link_reveals_exact_record() -> None:
    class ViewHost(App):
        def compose(self) -> ComposeResult:
            yield TrajectoryView("p1")

    app = ViewHost()
    async with app.run_test(size=(100, 30)) as pilot:
        view = app.query_one(TrajectoryView)
        view.state.upsert(
            (
                record("first", 1, request_id="shared"),
                record("second", 2, request_id="shared"),
            )
        )
        view._refresh()
        view.on_span_detail_record_link_clicked(SpanDetailRecordLinkClicked("first"))
        await pilot.pause()

        assert view.state.selected_id == "first"
        assert view.state.detail_id == "first"
