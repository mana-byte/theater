from __future__ import annotations

import asyncio

import pytest
from rich.cells import cell_len
from rich.console import Console
from rich.text import Text
from textual.app import App, ComposeResult
from textual.color import Color
from textual.coordinate import Coordinate
from textual.geometry import Size
from textual.widgets import Button, Input, RichLog, Select, SelectionList, Tab

from theater.constants.regie_trajectory import (
    FILTER_MAX_ROWS,
    LEDGER_OVERSCAN_ROWS,
    TIMELINE_LABEL_RIGHT_PADDING,
    TIMELINE_LABEL_WIDTH,
    TIMELINE_LANE_HEIGHT,
    TIMELINE_SPAN_MIN_WIDTH,
    TIMELINE_TURN_BOUNDARY_GLYPH,
    TRAJECTORY_SPAN_ROW_HEIGHT,
    TRAJECTORY_TABLE_CELL_PADDING,
)
from theater.constants.trajectory import TRAJECTORY_TOOLTIP_SUMMARY_MAX_CELLS
from theater.regie.trajectory.enums import FilterDimension, InspectorTab, OrderMode, TimelineLane
from theater.regie.trajectory.inspection.links import DETAIL_PARTICIPANT_META
from theater.regie.trajectory.inspection.project import detail_text, tabs_for_record
from theater.regie.trajectory.inspection.styled import build_span_details
from theater.regie.trajectory.models import decode_delta, decode_page
from theater.regie.trajectory.render.ordering import build_ordering
from theater.regie.trajectory.render.records import record_line, sanitize_text, tooltip_text
from theater.regie.trajectory.render.timeline import build_timeline_layout, timeline_lane
from theater.regie.trajectory.search import FilterCounts, search_records
from theater.regie.trajectory.view import TrajectoryParticipantSelected, TrajectoryView
from theater.regie.trajectory.widgets.filter_panel import FilterPanel
from theater.regie.trajectory.widgets.hover_card import TimelineHoverCard
from theater.regie.trajectory.widgets.ledger import (
    Ledger,
    LedgerOlderClicked,
    LedgerRecordClicked,
    LedgerRecordHovered,
    LedgerRetryClicked,
)
from theater.regie.trajectory.widgets.span_detail import SpanDetailPanel
from theater.regie.trajectory.widgets.timeline import (
    Timeline,
    TimelineSpanClicked,
    TimelineSpanHovered,
)
from theater.trajectory import (
    GroupKind,
    PanelState,
    PanelStateInfo,
    Timing,
    TimingProvenance,
    TrajectoryDelta,
    TrajectoryGroup,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryUpsert,
)


def wire_record(
    record_id: str,
    *,
    participant_id: str = "p1",
    index: int = 0,
    turn_id: str | None = "t1",
    step_id: str | None = None,
    lane: str = "model",
    kind: str = "assistant",
    summary: str | None = None,
    details: list[dict[str, object]] | None = None,
    links: list[dict[str, object]] | None = None,
    request_id: str | None = None,
    call_id: str | None = None,
    mcp_server: str | None = None,
    mcp_tool: str | None = None,
    timing: dict[str, object] | None = None,
    usage: dict[str, object] | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "record_id": record_id,
        "revision": 1,
        "participant_id": participant_id,
        "source_epoch": "epoch",
        "lane": lane,
        "kind": kind,
        "source": "claude",
        "summary": summary if summary is not None else f"record {index}",
        "status": "completed",
        "raw_index": index,
        "turn_id": turn_id,
    }
    if step_id is not None:
        result["step_id"] = step_id
    if details is not None:
        result["details"] = details
    if links is not None:
        result["links"] = links
    if request_id is not None:
        result["request_id"] = request_id
    if call_id is not None:
        result["call_id"] = call_id
    if mcp_server is not None:
        result["mcp_server"] = mcp_server
    if mcp_tool is not None:
        result["mcp_tool"] = mcp_tool
    if timing is not None:
        result["timing"] = timing
    if usage is not None:
        result["usage"] = usage
    return result


def record(
    record_id: str, *, index: int = 0, turn_id: str | None = "t1", **kwargs: object
) -> TrajectoryRecord:
    return TrajectoryRecord.from_wire(
        wire_record(record_id, index=index, turn_id=turn_id, **kwargs)
    )


class Host(App):
    def __init__(self, *, copied: list[str] | None = None) -> None:
        super().__init__()
        self.copied = copied if copied is not None else []

    def compose(self) -> ComposeResult:
        yield TrajectoryView("p1", copy_request=self.copied.append, id="trajectory")


async def populate(app: Host, records: list[TrajectoryRecord]) -> TrajectoryView:
    view = app.query_one(TrajectoryView)
    view.state.panel = PanelStateInfo(PanelState.READY, participant_state="live")
    view.state.upsert(records)
    view._refresh()
    return view


async def test_ledger_window_is_bounded_and_scroll_hit_testing_uses_offset() -> None:
    records = [record(f"r{index}", index=index, turn_id=f"t{index}") for index in range(100)]
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(TrajectoryView).state_store.page_size = 100
        await populate(app, records)
        ledger = app.query_one(Ledger)
        ledger._viewport_height = 5
        ledger.update_rows(records, search_records(records))
        await pilot.pause()

        assert ledger.rendered_record_count <= 5 + 2 * LEDGER_OVERSCAN_ROWS
        ledger.set_scroll_offset(10)
        await pilot.pause()
        assert ledger.entries[11].record_id == "r11"
        assert int(ledger.scroll_y) == 10 * TRAJECTORY_SPAN_ROW_HEIGHT
        assert ledger.rendered_record_count <= 5 + 2 * LEDGER_OVERSCAN_ROWS


async def test_ledger_prepend_preserves_selected_anchor_and_true_tail_clamp() -> None:
    original = [record(f"r{index}", index=index, turn_id=None) for index in range(20)]
    app = Host()
    async with app.run_test(size=(100, 30)):
        await populate(app, original)
        ledger = app.query_one(Ledger)
        ledger._viewport_height = 5
        ledger.update_rows(original, search_records(original), selected_id="r10")
        ledger.set_scroll_offset(5)
        old_offset = ledger._scroll_offset

        older = [record(f"old{index}", index=index, turn_id=None) for index in range(3)]
        combined = older + original
        ledger.update_rows(combined, search_records(combined), selected_id="r10")

        assert ledger._scroll_offset == old_offset + 3
        ledger.set_scroll_offset(10_000)
        assert ledger._scroll_offset == len(ledger.entries) - ledger.viewport_rows


async def test_ledger_sizes_non_summary_columns_to_displayed_content() -> None:
    payload = wire_record("long", index=1, turn_id=None)
    payload["source"] = "long-adapter-source"
    payload["status"] = "interrupted"
    item = TrajectoryRecord.from_wire(payload)
    app = Host()
    async with app.run_test(size=(100, 30)):
        await populate(app, [item])
        ledger = app.query_one(Ledger)
        columns = {column.key.value: column for column in ledger.ordered_columns}

        assert ledger.cell_padding == TRAJECTORY_TABLE_CELL_PADDING
        assert tuple(columns) == (
            Ledger.COLUMN_POSITION,
            Ledger.COLUMN_EVENT,
            Ledger.COLUMN_SUMMARY,
            Ledger.COLUMN_DURATION,
        )
        event = "◆ ASSISTANT"
        assert columns[Ledger.COLUMN_EVENT].width == cell_len(event)
        assert columns[Ledger.COLUMN_EVENT].get_render_width(ledger) == (
            cell_len(event) + 2 * TRAJECTORY_TABLE_CELL_PADDING
        )
        summary = ledger.get_cell("record:long", Ledger.COLUMN_SUMMARY)
        assert "INTERRUPTED" in summary.plain
        assert "long-adapter-source" not in summary.plain


async def test_turn_groups_stay_expanded_when_horizontal_keys_are_pressed() -> None:
    records = [
        record("before", index=0, turn_id="before"),
        record("hidden-1", index=1, turn_id="collapsed"),
        record("hidden-2", index=2, turn_id="collapsed"),
        record("after", index=3, turn_id="after"),
    ]
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = await populate(app, records)
        assert view._selected_visible_ids() == ("before", "hidden-1", "hidden-2", "after")
        view.state.select("hidden-1")
        view._handle_contextual_horizontal(-1)
        view._handle_contextual_horizontal(1)

        assert view._selected_visible_ids() == ("before", "hidden-1", "hidden-2", "after")
        assert view.state.selected_id == "hidden-1"


async def test_timeline_scroll_hit_testing_and_positioned_spans() -> None:
    records = [record(f"r{index}", index=index) for index in range(10)]
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = await populate(app, records)
        timeline = view.query_one(Timeline)
        timeline._viewport_width = TIMELINE_LABEL_WIDTH + 4
        timeline.update_records(records, selected_id=None)
        sixth = timeline.projection.span_for("r6")
        assert sixth is not None
        timeline.set_scroll_offset(sixth.x)

        model_middle = 1 + list(TrajectoryLane).index(TrajectoryLane.MODEL) * TIMELINE_LANE_HEIGHT
        assert timeline._record_at(TIMELINE_LABEL_WIDTH + 1, model_middle).record_id == "r6"
        assert timeline.scroll_span_into_view("r9") == timeline.tail_offset
        assert len(timeline.projection.spans) == len(records)
        assert {span.width for span in timeline.projection.spans} == {TIMELINE_SPAN_MIN_WIDTH}
        assert timeline.tail_offset > 0
        timeline.update_records(records, matched_ids=frozenset(), selected_id=None)
        normal_style = timeline._span_style(records[-1])
        timeline.set_hovered("r9")
        hovered_style = timeline._span_style(records[-1])
        assert hovered_style != normal_style
        assert hovered_style == timeline._lane_style(records[-1].lane, highlighted=True)


async def test_timeline_manual_scroll_continues_from_automatic_reveal() -> None:
    records = [record(f"r{index}", index=index, turn_id=None) for index in range(40)]
    app = Host()
    async with app.run_test(size=(50, 30)) as pilot:
        view = await populate(app, records)
        timeline = view.query_one(Timeline)
        await pilot.pause()

        timeline.set_scroll_offset(80)
        automatic_offset = timeline.horizontal_offset

        assert automatic_offset == 80
        assert timeline.scroll_target_x == automatic_offset

        timeline._scroll_left_for_pointer(animate=False)
        await pilot.pause()

        assert timeline.horizontal_offset == automatic_offset - app.scroll_sensitivity_x


async def test_timeline_projects_mcp_on_its_own_lane_and_preserves_duration_widths() -> None:
    records = [
        record("input", index=0, lane="input", kind="user"),
        record("model", index=1, lane="model", kind="assistant"),
        record("tools", index=2, lane="tools", kind="tool_call"),
        record(
            "mcp",
            index=3,
            lane="tools",
            kind="tool_call",
            mcp_server="grafana",
            mcp_tool="query_prometheus",
        ),
        record("theater", index=4, lane="theater", kind="spawn"),
    ]
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = await populate(app, records)
        timeline = view.query_one(Timeline)
        assert {span.lane for span in timeline.projection.spans} == set(TimelineLane)
        for lane_index, record_item in enumerate(records):
            span = timeline.projection.span_for(record_item.record_id)
            assert span is not None
            middle = 1 + lane_index * TIMELINE_LANE_HEIGHT
            assert (
                timeline._record_at(TIMELINE_LABEL_WIDTH + span.visual_start, middle) == record_item
            )
            assert timeline._record_at(TIMELINE_LABEL_WIDTH + span.x, middle) == record_item
            assert (
                timeline._record_at(TIMELINE_LABEL_WIDTH + span.visual_start, middle - 1)
                == record_item
            )

        assert timeline.projection.width == timeline._available_cells()
        assert timeline.projection.spans[0].x == 0
        assert timeline.projection.spans[-1].end == timeline.projection.width
        model_strip = timeline._lane_strip(
            TimelineLane.MODEL,
            0,
            timeline.projection.width,
        )
        assert model_strip.text == " " * timeline.projection.width
        assert any(segment.style and segment.style.bgcolor for segment in model_strip._segments)

        model_top = timeline._lane_strip(
            TimelineLane.MODEL,
            0,
            timeline.projection.width,
            row=0,
        )
        assert all(segment.style == timeline._component("track") for segment in model_top._segments)

    duration_records = (
        record(
            "short",
            index=0,
            timing={"start": 1.0, "end": 1.1, "provenance": "source"},
        ),
        record(
            "long",
            index=1,
            timing={"start": 1.0, "end": 2.0, "provenance": "source"},
        ),
    )
    duration = build_timeline_layout(duration_records, OrderMode.DURATION)
    assert duration.has_timing
    assert duration.span_for("long").width > duration.span_for("short").width
    assert all(span.width >= TIMELINE_SPAN_MIN_WIDTH for span in duration.spans)


async def test_timeline_hover_grows_span_without_markers() -> None:
    records = [
        record("first", index=0, request_id="request"),
        record("second", index=1, request_id="request"),
        record("call", index=2, lane="tools", kind="tool_call", call_id="tool"),
        record("result", index=3, lane="tools", kind="tool_result", call_id="tool"),
    ]
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = await populate(app, records)
        timeline = view.query_one(Timeline)

        view.on_timeline_span_hovered(TimelineSpanHovered("first"))

        assert timeline.hovered_id == "first"
        strip = timeline._lane_strip(TimelineLane.MODEL, 0, timeline.projection.width)
        hovered = timeline.projection.span_for("first")
        other = timeline.projection.span_for("second")
        assert hovered is not None and other is not None
        assert hovered.visual_start > hovered.x
        assert hovered.visual_end < hovered.end
        assert strip.text == " " * timeline.projection.width
        highlighted = timeline._lane_style(TimelineLane.MODEL, highlighted=True)
        styles = [segment.style for segment in strip._segments for _ in range(len(segment.text))]
        assert all(styles[x] == highlighted for x in range(hovered.x, hovered.end))
        assert styles[other.visual_start] != highlighted
        assert styles[other.x] != highlighted


async def test_timeline_precomputes_dense_overlap_paint_and_hit_segments() -> None:
    records = [
        record(
            f"r{index}",
            index=index,
            timing={"start": 1.0, "end": 2.0, "provenance": "source"},
        )
        for index in range(256)
    ]
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = await populate(app, records)
        timeline = view.query_one(Timeline)
        timeline.update_records(records, duration_mode=True)

        assert len(timeline._lane_visual_segments[TimelineLane.MODEL]) == 1
        assert len(timeline._lane_hit_segments[TimelineLane.MODEL]) == 1
        model_middle = 1 + list(TrajectoryLane).index(TrajectoryLane.MODEL) * TIMELINE_LANE_HEIGHT
        assert timeline._record_at(TIMELINE_LABEL_WIDTH, model_middle) == records[0]

        timeline.set_hovered(records[-1].record_id)
        strip = timeline._lane_strip(TimelineLane.MODEL, 0, 12)
        highlighted = timeline._lane_style(TrajectoryLane.MODEL, highlighted=True)
        styles = [segment.style for segment in strip._segments for _ in range(len(segment.text))]
        assert all(style == highlighted for style in styles)


async def test_tail_refresh_avoids_a_second_timeline_repaint(monkeypatch) -> None:
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = await populate(app, [record("r1")])
        timeline = view.query_one(Timeline)
        refreshes = 0
        original_refresh = timeline.refresh

        def count_refresh(*args, **kwargs):
            nonlocal refreshes
            refreshes += 1
            return original_refresh(*args, **kwargs)

        monkeypatch.setattr(timeline, "refresh", count_refresh)
        view._refresh(recompute=False)

        assert refreshes == 1


async def test_live_updates_defer_hidden_ledger_work_until_details_close(monkeypatch) -> None:
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = await populate(app, [record("r1")])
        view._open_details("r1")
        ledger = view.query_one(Ledger)
        updates = 0
        original_update_rows = ledger.update_rows

        def count_update_rows(*args, **kwargs):
            nonlocal updates
            updates += 1
            return original_update_rows(*args, **kwargs)

        monkeypatch.setattr(ledger, "update_rows", count_update_rows)
        view.state.upsert([record("r2", index=2, turn_id=None)])
        view._refresh()

        assert updates == 0
        assert view.state.detail_id == "r1"

        view._close_details()

        assert updates == 1
        assert ledger.get_row_index("record:r2") is not None


async def test_timeline_uses_two_rows_per_lane_and_marks_new_turns() -> None:
    records = [
        record("first", index=0, turn_id="turn-1"),
        record("same-turn", index=1, turn_id="turn-1"),
        record("next-turn", index=2, turn_id="turn-2"),
    ]
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = await populate(app, records)
        timeline = view.query_one(Timeline)
        next_span = timeline.projection.span_for("next-turn")
        assert next_span is not None
        strip = timeline._lane_strip(TimelineLane.MODEL, 0, timeline.projection.width)

        assert TIMELINE_LANE_HEIGHT == 2
        assert timeline.virtual_size.height == len(TimelineLane) * TIMELINE_LANE_HEIGHT
        assert strip.text[next_span.x] == TIMELINE_TURN_BOUNDARY_GLYPH


async def test_timeline_lane_labels_are_right_aligned() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = await populate(app, [record("model", index=0)])
        timeline = view.query_one(Timeline)
        model_middle = 1 + list(TimelineLane).index(TimelineLane.MODEL) * TIMELINE_LANE_HEIGHT

        line = timeline.render_line(model_middle)

        assert line.text[:TIMELINE_LABEL_WIDTH] == "MODEL".rjust(
            TIMELINE_LABEL_WIDTH - TIMELINE_LABEL_RIGHT_PADDING
        ).ljust(TIMELINE_LABEL_WIDTH)


def test_timeline_layout_reflows_existing_events_to_available_width() -> None:
    records = tuple(record(f"r{index}", index=index) for index in range(3))
    initial = build_timeline_layout(records[:2], OrderMode.ORDER, minimum_width=18)
    updated = build_timeline_layout(records, OrderMode.ORDER, minimum_width=18)

    assert [span.width for span in initial.spans] == [9, 9]
    assert [span.width for span in updated.spans] == [6, 6, 6]
    assert updated.spans[0].x == 0
    assert updated.spans[-1].end == 18


def test_projection_cache_and_hover_path_do_not_recompute_search() -> None:
    async def scenario() -> None:
        app = Host()
        async with app.run_test(size=(100, 30)):
            view = await populate(app, [record("r1"), record("r2", index=2, turn_id=None)])
            projection = view.projection
            key = projection.search_key
            cache_sizes = (
                len(projection.search_cache.corpus),
                len(projection.search_cache.query_scores),
            )
            assert view.search_result is projection.search_result
            assert projection.search_key == key
            view.on_ledger_record_hovered(type("Hover", (), {"record_id": "r1"})())
            assert projection.search_key == key
            assert (
                len(projection.search_cache.corpus),
                len(projection.search_cache.query_scores),
            ) == cache_sizes

    asyncio.run(scenario())


async def test_search_input_keeps_printable_navigation_keys() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        view = await populate(app, [record("r1"), record("r2", index=2, turn_id=None)])
        view.action_open_search()
        await pilot.press("j")
        assert view.state.query == "j"
        assert app.focused is app.query_one("#trajectory-search", Input)
        await pilot.press(*"klfdr y")
        assert view.state.query == "jklfdr y"
        assert app.query_one("#trajectory-search", Input).has_focus


async def test_timeline_and_ledger_share_group_flattened_order() -> None:
    records = [
        record("first", index=1, turn_id="t1"),
        record("between", index=2, turn_id=None, lane="theater", kind="theater"),
        record("last", index=3, turn_id="t2"),
    ]
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = await populate(app, records)
        timeline = view.query_one(Timeline)
        ledger = view.query_one(Ledger)
        ledger_ids = tuple(
            entry.record_id for entry in ledger.entries if entry.record_id is not None
        )

        assert timeline.span_ids == view.search_result.record_ids == ledger_ids
        assert timeline.span_ids == ("first", "between", "last")


async def test_timeline_and_ledger_preserve_nested_group_unit_chronology() -> None:
    records = [
        record("step-first", index=1, turn_id="t1", step_id="s1"),
        record("direct-middle", index=2, turn_id="t1"),
        record("step-late", index=3, turn_id="t1", step_id="s2"),
        record("direct-last", index=4, turn_id="t1"),
    ]
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = await populate(app, records)
        ledger = view.query_one(Ledger)
        ledger_ids = tuple(
            entry.record_id for entry in ledger.entries if entry.record_id is not None
        )

        assert view.query_one(Timeline).span_ids == ledger_ids
        assert ledger_ids == ("step-first", "direct-middle", "step-late", "direct-last")


def test_ordering_emits_many_nested_group_records_once_in_source_order() -> None:
    records = [record(f"r{index}", index=index) for index in range(128)]
    group = TrajectoryGroup(
        group_id="group-0",
        kind=GroupKind.STEP,
        label="Group 0",
        record_ids=("r0",),
    )
    for index in range(1, len(records)):
        group = TrajectoryGroup(
            group_id=f"group-{index}",
            kind=GroupKind.STEP,
            label=f"Group {index}",
            record_ids=(f"r{index}",),
            children=(group,),
        )

    ordered = build_ordering(records, (group,)).records

    assert tuple(record.record_id for record in ordered) == tuple(
        record.record_id for record in records
    )
    assert len({record.record_id for record in ordered}) == len(records)


async def test_duration_mode_marks_only_independently_reported_intervals() -> None:
    records = [
        record("missing", index=0, summary="missing timing"),
        record(
            "derived",
            index=1,
            summary="derived timing",
            timing={"duration_ms": 10, "provenance": "derived"},
        ),
        record(
            "source",
            index=2,
            summary="source timing",
            timing={"duration_ms": 10, "provenance": "source"},
        ),
    ]
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = await populate(app, records)
        view.state.pause_follow()
        view.state.select("derived")
        view.action_toggle_mode()
        ledger = app.query_one(Ledger)
        missing = ledger.get_cell("record:missing", Ledger.COLUMN_DURATION)
        derived = ledger.get_cell("record:derived", Ledger.COLUMN_DURATION)
        source = ledger.get_cell("record:source", Ledger.COLUMN_DURATION)
        assert isinstance(missing, Text)
        assert isinstance(derived, Text)
        assert isinstance(source, Text)
        assert missing.plain.strip() == "—"
        assert derived.plain.strip() == source.plain.strip() == "10ms"
        derived_style = derived.get_style_at_offset(Console(), 1)
        source_style = source.get_style_at_offset(Console(), 1)
        assert not derived_style.bold
        assert source_style.bold
        assert source_style.dim
        assert view.state.selected_id == "derived"
        assert view.query_one(Timeline).span_ids == ("missing", "derived", "source")


async def test_filter_panel_has_selectable_counts_and_filters_records() -> None:
    records = [
        record("model", index=1),
        record("tool", index=2, lane="tools", kind="tool_call"),
    ]
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = await populate(app, records)
        panel = view.query_one(FilterPanel)
        view.action_toggle_filters()
        assert any(
            dimension is FilterDimension.LANE and value == "tools"
            for dimension, value in panel.options
        )
        selection_list = panel.query_one(SelectionList)
        tools_index = panel.options.index((FilterDimension.LANE, "tools"))
        tools_prompt = selection_list.get_option_at_index(tools_index).prompt
        assert "tools" in tools_prompt.plain and "1" in tools_prompt.plain
        view.on_filter_value_clicked(
            type("Filter", (), {"dimension": FilterDimension.LANE, "value": "tools"})()
        )
        assert view.search_result.record_ids == ("tool",)


def test_duration_mode_changes_render_without_reordering_and_preserves_literals() -> None:
    item = TrajectoryRecord.from_wire(
        wire_record(
            "r1",
            summary="[literal] \\ data",
            timing={"duration_ms": 1250, "provenance": "source"},
        )
    )
    order = record_line(item, 1, duration_mode=False).plain
    duration = record_line(item, 1, duration_mode=True).plain
    assert order != duration
    assert "dur" in duration
    assert sanitize_text("[literal] \\ data") == "[literal] \\ data"


def test_timeline_tooltip_flattens_and_bounds_large_summaries() -> None:
    item = TrajectoryRecord.from_wire(
        wire_record("r1", summary="first line\n" + "界" * 300 + "\nlast line")
    )

    lines = tooltip_text(item).splitlines()

    assert len(lines) == 3
    assert cell_len(lines[1]) <= TRAJECTORY_TOOLTIP_SUMMARY_MAX_CELLS
    assert lines[1].endswith("…")


def test_timeline_tooltip_surfaces_model_timing_and_usage() -> None:
    item = TrajectoryRecord.from_wire(
        wire_record(
            "r1",
            timing={
                "start": 10,
                "first_token": 10.25,
                "end": 12,
                "duration_ms": 2_000,
                "provenance": "source",
            },
            usage={
                "model": "model-x",
                "input_tokens": 1_200,
                "output_tokens": 34,
                "reasoning_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "cost_usd": 0.1234,
            },
        )
    )

    heading, _summary, metrics = tooltip_text(item).splitlines()

    assert heading == "◆ ASSISTANT · claude · model-x · completed"
    assert metrics == "total 2.0s · TTFT 250ms · generation 1.8s · in 1.2K · out 34 · cost $0.1234"


def test_timeline_tooltip_labels_observed_duration_and_point_time() -> None:
    item = TrajectoryRecord.from_wire(wire_record("r1"))
    estimated = Timing(
        start=10,
        end=12,
        duration_ms=2_000,
        provenance=TimingProvenance.OBSERVED,
    )
    point = Timing(end=12, provenance=TimingProvenance.OBSERVED)

    estimated_metrics = tooltip_text(item, timing=estimated, timing_scope="request").splitlines()[2]
    point_metrics = tooltip_text(item, timing=point).splitlines()[2]

    assert estimated_metrics == "request ~2.0s observed"
    assert "observed" in point_metrics
    assert "timing unavailable" not in point_metrics


async def test_timeline_hover_prefers_tool_operation_timing() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)):
        call = record(
            "call",
            index=1,
            lane="tools",
            kind="tool_call",
            call_id="shared",
            timing={"start": 10, "provenance": "observed"},
        )
        result = record(
            "result",
            index=2,
            lane="tools",
            kind="tool_result",
            call_id="shared",
            timing={"end": 12, "provenance": "observed"},
        )
        view = await populate(app, [call, result])

        timing, scope = view._hover_timing("call")

        assert scope == "tool"
        assert timing == Timing(
            start=10,
            end=12,
            duration_ms=2_000,
            provenance=TimingProvenance.OBSERVED,
        )


async def test_repeated_hover_reuses_tooltip_without_reprocessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        item = record("r1")
        view = await populate(app, [item])
        timeline = view.query_one(Timeline)
        card = view.query_one(TimelineHoverCard)
        calls = 0

        def render_tooltip(_record: TrajectoryRecord, **_kwargs) -> str:
            nonlocal calls
            calls += 1
            return "tooltip"

        monkeypatch.setattr(
            "theater.regie.trajectory.widgets.hover_card.tooltip_text", render_tooltip
        )
        lane_y = tuple(TimelineLane).index(timeline_lane(item)) * TIMELINE_LANE_HEIGHT + 1
        await pilot.hover(timeline, offset=(TIMELINE_LABEL_WIDTH + 2, lane_y))
        await pilot.hover(timeline, offset=(TIMELINE_LABEL_WIDTH + 3, lane_y))

        assert card.display
        assert card.record_id == "r1"
        assert card.region.bottom <= timeline.region.y
        assert timeline.tooltip is None

        timeline._set_hover(None)
        timeline._set_hover(item)

        assert calls == 1


@pytest.mark.parametrize(("width", "record_index"), [(100, 0), (100, -1), (50, -1)])
async def test_timeline_hover_card_stays_inside_terminal_edges(
    width: int,
    record_index: int,
) -> None:
    app = Host()
    async with app.run_test(size=(width, 30)) as pilot:
        records = [record(f"r{index}", index=index, summary="x" * 200) for index in range(10)]
        view = await populate(app, records)
        timeline = view.query_one(Timeline)
        card = view.query_one(TimelineHoverCard)
        item = records[record_index]
        anchor = timeline.hover_anchor(item.record_id)
        assert anchor is not None
        lane_y = tuple(TimelineLane).index(timeline_lane(item)) * TIMELINE_LANE_HEIGHT + 1

        await pilot.hover(timeline, offset=(anchor.x - timeline.region.x, lane_y))
        await pilot.pause()

        assert card.display
        assert card.region.x >= app.screen.region.x
        assert card.region.right <= app.screen.region.right


async def test_clicks_and_movement_pause_tail_but_hover_does_not() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = await populate(app, [record("r1"), record("r2", index=2, turn_id=None)])
        view.on_timeline_span_clicked(TimelineSpanClicked("r1"))
        assert not view.state.follow_tail
        view.state.resume_follow()
        view.on_ledger_record_hovered(type("Hover", (), {"record_id": "r1"})())
        assert view.state.follow_tail
        view.on_ledger_record_clicked(type("Click", (), {"record_id": "r1"})())
        assert not view.state.follow_tail
        view.on_ledger_record_clicked(type("Click", (), {"record_id": "r2"})())
        assert view.state.follow_tail
        view.state.resume_follow()
        view.action_timeline_previous()
        assert not view.state.follow_tail


async def test_reaching_visible_bottom_resumes_tail_and_follows_new_records() -> None:
    records = [record(f"r{index}", index=index, turn_id=None) for index in range(12)]
    app = Host()
    async with app.run_test(size=(100, 24)):
        view = await populate(app, records)
        ledger = view.query_one(Ledger)
        ledger._viewport_height = 3
        view.state.select("r10")
        view.state.pause_follow()
        view._sync_selection()

        view.action_select_next()

        assert view.state.selected_id == "r11"
        assert view.state.follow_tail
        assert view.state.new_count == 0

        newest = record("r12", index=12, turn_id=None)
        view.state.apply_follow(
            TrajectoryDelta(stream_id="stream", upserts=(TrajectoryUpsert(newest),))
        )
        view._refresh()

        assert view.state.selected_id == "r12"
        assert ledger._selected_id == "r12"
        assert ledger._scroll_offset > 0


async def test_selection_and_hover_use_incremental_widget_updates(monkeypatch) -> None:
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        view = await populate(app, [record("r1"), record("r2", index=2, turn_id=None)])
        refreshes = 0

        def count_refresh(*, recompute: bool = True) -> None:
            nonlocal refreshes
            refreshes += 1

        monkeypatch.setattr(view, "_refresh", count_refresh)
        timeline = view.query_one(Timeline)
        ledger = view.query_one(Ledger)

        timeline.set_hovered("r1")
        await pilot.pause()
        assert view.state.hovered_id is None
        refreshes = 0

        view.on_ledger_record_hovered(LedgerRecordHovered("r1"))
        view.on_ledger_record_clicked(LedgerRecordClicked("r1"))

        assert refreshes == 0
        assert view.state.hovered_id == "r1"
        assert timeline.hovered_id == "r1"
        assert timeline.selected_id == "r1"
        assert ledger._hovered_id == "r1"
        assert view.state.detail_id == "r1"
        assert view.query_one(SpanDetailPanel).record_id == "r1"
        assert ledger.has_class("-hidden")
        summary = ledger.get_cell("record:r1", Ledger.COLUMN_SUMMARY)
        assert isinstance(summary, Text)
        assert "underline" not in str(summary.get_style_at_offset(Console(), 1))


async def test_ledger_pages_with_shift_h_and_shift_l() -> None:
    records = [record(f"r{index}", index=index, turn_id=None) for index in range(5)]
    app = Host()
    async with app.run_test(size=(100, 24)) as pilot:
        view = app.query_one(TrajectoryView)
        view.state_store.page_size = 2
        view.state.panel = PanelStateInfo(PanelState.READY, participant_state="live")
        view.state.upsert(records)
        view._refresh()
        ledger = view.query_one(Ledger)

        assert view.state.ledger_page == 2
        assert tuple(entry.record_id for entry in ledger.entries if entry.record_id) == ("r4",)
        assert view.query_one("#trajectory-page", Select).value == 2
        assert "5 items" in str(view.query_one("#trajectory-page-range").content)

        await pilot.press("shift+h")
        assert view.state.ledger_page == 1
        assert tuple(entry.record_id for entry in ledger.entries if entry.record_id) == (
            "r2",
            "r3",
        )
        position = ledger.get_cell("record:r2", Ledger.COLUMN_POSITION)
        assert isinstance(position, Text)
        assert position.plain.strip().endswith("3")
        assert not view.state.follow_tail
        assert view.query_one("#trajectory-page", Select).value == 1

        await pilot.press("shift+l")
        assert view.state.ledger_page == 2
        assert view.state.selected_id == "r4"
        assert view.state.follow_tail


async def test_ledger_selection_crosses_page_boundaries_with_j_and_k() -> None:
    records = [record(f"r{index}", index=index, turn_id=None) for index in range(5)]
    app = Host()
    async with app.run_test(size=(100, 24)) as pilot:
        view = app.query_one(TrajectoryView)
        view.state_store.page_size = 2
        view.state.panel = PanelStateInfo(PanelState.READY, participant_state="live")
        view.state.upsert(records)
        view._refresh()

        await pilot.press("shift+h", "k")
        assert view.state.ledger_page == 1
        assert view.state.selected_id == "r2"

        await pilot.press("k")
        assert view.state.ledger_page == 0
        assert view.state.selected_id == "r1"

        await pilot.press("j")
        assert view.state.ledger_page == 1
        assert view.state.selected_id == "r2"

        await pilot.press("j")
        assert view.state.ledger_page == 1
        assert view.state.selected_id == "r3"

        await pilot.press("j")
        assert view.state.ledger_page == 2
        assert view.state.selected_id == "r4"


async def test_footer_page_buttons_and_selector_change_pages() -> None:
    records = [record(f"r{index}", index=index, turn_id=None) for index in range(5)]
    app = Host()
    async with app.run_test(size=(100, 24)) as pilot:
        view = app.query_one(TrajectoryView)
        view.state_store.page_size = 2
        view.state.panel = PanelStateInfo(PanelState.READY, participant_state="live")
        view.state.upsert(records)
        view._refresh()
        previous = view.query_one("#trajectory-page-previous", Button)
        following = view.query_one("#trajectory-page-next", Button)
        selector = view.query_one("#trajectory-page", Select)

        assert selector.value == 2
        assert following.disabled
        await pilot.click(previous)
        assert view.state.ledger_page == 1
        assert not previous.disabled
        assert not following.disabled
        await pilot.click(following)
        assert view.state.ledger_page == 2

        await pilot.click(selector)
        assert selector.expanded
        await pilot.press("home", "enter")
        await pilot.pause()

        assert view.state.ledger_page == 0
        assert selector.value == 0
        assert previous.disabled


async def test_trajectory_controls_use_muted_theme_interaction_colors() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        view = await populate(app, [record("r1", turn_id=None)])
        accent = Color.parse(app.get_css_variables()["accent"])

        search = view.query_one("#trajectory-search-action", Button)
        await pilot.hover(search)
        assert search.styles.background == accent.with_alpha(0.1)

        selector = view.query_one("#trajectory-view-action", Select)
        current = selector.query_one("SelectCurrent")
        await pilot.hover(current)
        assert current.styles.background == accent.with_alpha(0.1)
        await pilot.click(selector)
        assert selector.query_one("SelectOverlay").styles.background_tint == Color(0, 0, 0, 0)
        await pilot.press("escape")

        view.action_toggle_filters()
        await pilot.pause()
        clear = view.query_one("#trajectory-filter-clear", Button)
        await pilot.hover(clear)
        assert clear.styles.background == accent.with_alpha(0.1)

        view.action_toggle_filters()
        view.action_open_details()
        await pilot.pause()
        close = view.query_one("#trajectory-span-detail-close", Button)
        await pilot.hover(close)
        assert close.styles.background == accent.with_alpha(0.15)
        active_tab = view.query_one("#trajectory-span-detail-tabs Tab.-active", Tab)
        assert active_tab.styles.background == accent.with_alpha(0.2)


def test_context_tabs_render_matching_formats_and_copy_exactly() -> None:
    item = record(
        "system",
        kind="system",
        details=[
            {
                "name": "current",
                "format": "json",
                "value": {"text": '{"z": 1, "a": [2]}', "omitted_bytes": 0},
            },
            {
                "name": "previous",
                "format": "markdown",
                "value": {"text": "[old] \\ path", "omitted_bytes": 0},
            },
            {
                "name": "diff",
                "format": "diff",
                "value": {"text": "--- old\n+++ new\n@@ -1 +1 @@", "omitted_bytes": 0},
            },
        ],
    )
    assert tabs_for_record(item) == (
        InspectorTab.CURRENT,
        InspectorTab.PREVIOUS,
        InspectorTab.DIFF,
    )
    current = detail_text(item, InspectorTab.CURRENT)
    previous = detail_text(item, InspectorTab.PREVIOUS)
    diff = detail_text(item, InspectorTab.DIFF)
    assert '"a": [' in current and "No current" not in current
    assert "[old] \\ path" in previous and "No previous" not in previous
    assert "--- old" in diff and "No diff" not in diff


def test_span_details_use_only_contextual_tabs() -> None:
    item = record(
        "system",
        kind="system",
        details=[
            {
                "name": "current",
                "format": "json",
                "value": {"text": '{"mode": "new"}', "omitted_bytes": 0},
            }
        ],
    )
    details = build_span_details(item, InspectorTab.CURRENT)
    assert details.tabs == (InspectorTab.CURRENT,)
    assert details.tab is InspectorTab.CURRENT


def test_span_details_render_model_prose_as_markdown() -> None:
    item = record(
        "assistant",
        summary="## Result\n\n- **Passed** checks\n- Read `src/app.py`",
    )

    details = build_span_details(item, InspectorTab.OUTPUT)
    console = Console(width=60, record=True)
    console.print(details.content)

    assert "• Passed checks" in console.export_text()
    assert "**Passed**" in details.copy_text
    assert "No output supplied" not in details.copy_text
    assert details.tabs == (InspectorTab.SUMMARY, InspectorTab.OUTPUT)


async def test_span_detail_tab_and_content_update_without_rebuilding_ledger(monkeypatch) -> None:
    item = record(
        "r1",
        details=[
            {
                "name": "output",
                "format": "text",
                "value": {"text": "line\n" * 1000, "omitted_bytes": 0},
            }
        ],
    )
    app = Host()
    async with app.run_test(size=(80, 24)) as pilot:
        view = await populate(app, [item])
        view._open_details("r1")
        await pilot.pause()
        ledger = view.query_one(Ledger)
        panel = view.query_one(SpanDetailPanel)
        rebuilds = 0
        original = ledger._rebuild

        def count_rebuild(*, preserve_scroll: bool = True) -> None:
            nonlocal rebuilds
            rebuilds += 1
            original(preserve_scroll=preserve_scroll)

        monkeypatch.setattr(ledger, "_rebuild", count_rebuild)
        panel.set_tab(InspectorTab.OUTPUT)
        await pilot.pause()

        assert rebuilds == 0
        assert panel.tab is InspectorTab.OUTPUT
        assert "output: line" in panel.copy_text

        payload = item.to_wire()
        payload["revision"] = 2
        payload["details"] = [
            {
                "name": "output",
                "format": "text",
                "value": {"text": "updated", "omitted_bytes": 0},
            }
        ]
        updated = TrajectoryRecord.from_wire(payload)
        view.state.upsert([updated])
        view._refresh()
        assert rebuilds == 0
        assert "output: updated" in panel.copy_text
        await pilot.pause()
        await pilot.pause()
        log = panel.query_one("#trajectory-span-detail-content-output", RichLog)
        assert "updated" in "\n".join(strip.text for strip in log.lines)


async def test_span_detail_preserves_scroll_during_live_request_updates() -> None:
    output = "\n".join(f"line {index}" for index in range(120))
    first = record(
        "r1",
        request_id="request-1",
        details=[
            {
                "name": "output",
                "format": "text",
                "value": {"text": output, "omitted_bytes": 0},
            }
        ],
    )
    app = Host()
    async with app.run_test(size=(80, 24)) as pilot:
        view = await populate(app, [first])
        view._open_details("r1")
        await pilot.pause()
        panel = view.query_one(SpanDetailPanel)
        log = panel.query_one("#trajectory-span-detail-content-summary", RichLog)
        log.scroll_to(y=20, animate=False, force=True)
        await pilot.pause()
        scroll_y = float(log.scroll_y)
        rendered_lines = tuple(log.lines)

        view.state.upsert([record("r2", index=2, request_id="request-1")])
        view._refresh()

        # The replacement is deferred until after refresh. Live request updates
        # must not blank the active detail log or flash its loading layer.
        assert tuple(log.lines) == rendered_lines
        assert not panel.query_one("#trajectory-span-detail-loading").display
        await pilot.pause()

        assert scroll_y > 0
        assert float(log.scroll_y) == scroll_y
        assert panel.record_id == "r1"

        view._open_details("r2")
        await pilot.pause()
        assert float(log.scroll_y) == 0


@pytest.mark.asyncio
async def test_filter_cursor_is_styled_and_scrolled_into_view() -> None:
    class FilterHost(App):
        def compose(self) -> ComposeResult:
            yield FilterPanel()

    app = FilterHost()
    async with app.run_test(size=(80, FILTER_MAX_ROWS // 2 + 2)) as pilot:
        panel = app.query_one(FilterPanel)
        panel.update_filters(
            FilterCounts(
                lanes=dict.fromkeys(TrajectoryLane, 1),
                kinds={},
                statuses={},
                sources={f"source-{index}": 1 for index in range(20)},
            ),
            lanes=set(),
            kinds=set(),
            statuses=set(),
            sources=set(),
        )
        selection_list = panel.query_one(SelectionList)
        panel.focus_options()
        await pilot.press(*(["j"] * (len(panel.options) - 1)))
        assert panel._cursor == len(panel.options) - 1
        assert panel._scroll_offset > 0
        assert panel._scroll_offset <= panel._cursor < panel._scroll_offset + FILTER_MAX_ROWS
        assert selection_list.highlighted == len(panel.options) - 1
        assert app.focused is selection_list


@pytest.mark.asyncio
async def test_links_are_exact_and_callback_excludes_fallback() -> None:
    item = record(
        "system",
        kind="system",
        links=[
            {"participant_id": "p", "relation": "child", "direction": "outgoing"},
            {"participant_id": "p-long", "relation": "child", "direction": "outgoing"},
        ],
    )
    details = build_span_details(item, InspectorTab.SUMMARY)
    linked = {
        meta[DETAIL_PARTICIPANT_META]
        for span in details.content.spans
        if (meta := getattr(span.style, "meta", {})) and DETAIL_PARTICIPANT_META in meta
    }
    assert linked == {"p", "p-long"}

    called: list[str] = []

    class LinkViewHost(App):
        def compose(self) -> ComposeResult:
            yield TrajectoryView("p1", participant_link=called.append)

        def on_trajectory_participant_selected(
            self, _message: TrajectoryParticipantSelected
        ) -> None:
            called.append("fallback")

    link_app = LinkViewHost()
    async with link_app.run_test(size=(80, 40)) as pilot:
        view = link_app.query_one(TrajectoryView)
        view.state.upsert([item])
        view._refresh()
        view._open_details("system")
        await pilot.pause()
        log = view.query_one(
            "#trajectory-span-detail-content-current",
            RichLog,
        )
        await pilot.click(log, offset=(3, 4))
        await pilot.pause()
    assert called == ["p"]


@pytest.mark.asyncio
async def test_retry_action_inside_error_row_is_clickable_and_keyboard_accessible() -> None:
    class RetryHost(App):
        def __init__(self) -> None:
            super().__init__()
            self.retries = 0

        def compose(self) -> ComposeResult:
            yield Ledger()

        def on_ledger_retry_clicked(self, _message: LedgerRetryClicked) -> None:
            self.retries += 1

    app = RetryHost()
    async with app.run_test(size=(80, 20)) as pilot:
        ledger = app.query_one(Ledger)
        ledger.update_rows([], search_records([]), retry_message="try again")
        retry = ledger.get_cell(Ledger.RETRY_KEY, Ledger.COLUMN_SUMMARY)
        assert isinstance(retry, Text)
        assert retry.plain.strip() == "try again · ↻ Retry"
        row = ledger.get_row_index(Ledger.RETRY_KEY)
        assert ledger.ordered_rows[row].height == 2
        column = ledger.get_column_index(Ledger.COLUMN_SUMMARY)
        region = ledger._get_cell_region(Coordinate(row, column))
        await pilot.click(
            ledger,
            offset=(region.x + 2, region.y - int(ledger.scroll_y) + 1),
        )
        assert app.retries == 1
        ledger.focus()
        ledger.move_cursor(row=row, column=column, animate=False)
        await pilot.press("enter")
        assert app.retries == 2


@pytest.mark.asyncio
async def test_earlier_history_row_is_clickable_once() -> None:
    class OlderHost(App):
        def __init__(self) -> None:
            super().__init__()
            self.loads = 0

        def compose(self) -> ComposeResult:
            yield Ledger()

        def on_ledger_older_clicked(self, _message: LedgerOlderClicked) -> None:
            self.loads += 1

    app = OlderHost()
    async with app.run_test(size=(80, 20)) as pilot:
        ledger = app.query_one(Ledger)
        ledger.update_rows([], search_records([]), has_older=True)
        assert ledger.ordered_rows[ledger.get_row_index(Ledger.OLDER_KEY)].height == 2
        await pilot.click(ledger, offset=(2, ledger.header_height + 1))
        assert app.loads == 1


def test_oversized_canonical_page_and_delta_are_rejected() -> None:
    records = [wire_record(str(index), summary="x" * 16_000) for index in range(70)]
    with pytest.raises(ValueError):
        decode_page(
            {
                "panel_state": {"state": "ready"},
                "stream_id": "stream",
                "cursor": "c1",
                "records": records,
            }
        )
    with pytest.raises(ValueError):
        decode_delta(
            {
                "stream_id": "stream",
                "cursor": "c2",
                "upserts": [{"record": record} for record in records],
            }
        )


def test_canonical_timing_fields_are_used() -> None:
    timing = Timing(1, 2, 1000, TimingProvenance.SOURCE)
    assert timing.start == 1
    assert timing.end == 2
    assert timing.duration_ms == 1000


def test_duration_mode_uses_derived_timing_for_split_source_records() -> None:
    """Duration mode must use the derived operation interval when a record's own
    timing is incomplete.

    Vibe tool calls carry only a start and tool results only a duration_ms, so
    neither record independently satisfies supports_duration_interval. The
    tool index derives a complete interval from the pair; the timeline must
    consume that derived timing instead of silently falling back to sequence
    mode.
    """
    call = record(
        "call",
        index=0,
        lane="tools",
        kind="tool_call",
        call_id="tool",
        timing={"start": 1.0, "provenance": "observed"},
    )
    result = record(
        "result",
        index=1,
        lane="tools",
        kind="tool_result",
        call_id="tool",
        timing={"duration_ms": 500.0, "provenance": "observed"},
    )
    records = (call, result)

    # Without a derived-timing resolver: no record has a usable own interval,
    # so Duration mode falls back to sequence layout (the bug).
    fallback = build_timeline_layout(records, OrderMode.DURATION)
    assert not fallback.has_timing
    assert all(not span.timed for span in fallback.spans)

    # The tool index derives a complete interval (start=1.0, end=1.5) from the
    # call's start and the result's duration_ms.
    derived = Timing(start=1.0, end=1.5, duration_ms=500.0, provenance=TimingProvenance.OBSERVED)
    timing_for = {"call": derived, "result": derived}.get

    laid = build_timeline_layout(records, OrderMode.DURATION, timing_for=timing_for)
    assert laid.has_timing
    assert all(span.timed for span in laid.spans)
    # The two records share the same operation interval, so they occupy the
    # same width band rather than the equal-width sequence fallback.
    assert laid.span_for("call").width == laid.span_for("result").width


def test_duration_mode_resolver_does_not_time_a_derived_provenance_record() -> None:
    """The resolver must not bypass supports_duration_interval for own timing.

    A DERIVED-provenance record is rejected by the own-timing gate. The resolver
    must not hand back the record's own timing through a fallback, or such a
    record would be plotted as timed despite being ineligible.
    """
    derived_only = record(
        "derived",
        index=0,
        timing={"duration_ms": 10, "provenance": "derived"},
    )
    records = (derived_only,)

    # A resolver that returns the record's own timing (the bug shape) would
    # silently time it. The view's _timing_for returns None for records with no
    # tool/request membership, so this is the contract the timeline relies on.
    laid = build_timeline_layout(records, OrderMode.DURATION, timing_for=lambda _id: None)
    assert not laid.has_timing
    assert all(not span.timed for span in laid.spans)


def test_duration_mode_resolver_skips_point_event_request_members() -> None:
    """Point events must not inherit a request-wide interval from the resolver.

    A request spans a user message, model turns, and tool calls. Point events
    (USER/SYSTEM) are members of the request but have no duration to plot; the
    resolver must skip them so they do not render as duplicate timed bars.
    """
    user = record(
        "user",
        index=0,
        kind="user",
        request_id="request",
    )
    model = record(
        "model",
        index=1,
        request_id="request",
        timing={"start": 1.0, "end": 2.0, "provenance": "source"},
    )
    records = (user, model)

    # The request carries a derived interval; the model record already has its
    # own usable interval, so only the user point event would be a candidate
    # for the resolver. The view's _timing_for skips point events.
    timing_for = {"user": None, "model": None}.get  # point event skipped
    laid = build_timeline_layout(records, OrderMode.DURATION, timing_for=timing_for)
    # model is timed by its own interval; user stays untimed.
    assert laid.has_timing
    assert laid.span_for("model").timed
    assert not laid.span_for("user").timed


async def test_timeline_resize_preserves_timing_for_resolver() -> None:
    """on_resize must re-supply timing_for so Duration mode survives a resize.

    Regression: on_resize rebuilt via update_records without forwarding the
    resolver, reverting a timed Duration layout to the sequence fallback.
    """
    call = record(
        "call",
        index=0,
        lane="tools",
        kind="tool_call",
        call_id="tool",
        timing={"start": 1.0, "provenance": "observed"},
    )
    result = record(
        "result",
        index=1,
        lane="tools",
        kind="tool_result",
        call_id="tool",
        timing={"duration_ms": 500.0, "provenance": "observed"},
    )
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        view = await populate(app, [call, result])
        view.state.order_mode = OrderMode.DURATION
        view._refresh()
        timeline = view.query_one(Timeline)
        await pilot.pause()
        assert timeline.projection.has_timing, (
            "Duration mode should be timed via the derived resolver"
        )

        # Resize the timeline narrower; on_resize rebuilds the layout.
        old_width = timeline._available_cells()
        timeline._viewport_width = max(1, old_width // 2)
        timeline.on_resize(
            type(
                "E",
                (),
                {"size": Size(timeline._viewport_width, timeline.size.height)},
            )()
        )
        await pilot.pause()

        assert timeline.projection.has_timing, (
            "timing_for must survive on_resize; Duration mode must not fall back"
        )
