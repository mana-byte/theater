from __future__ import annotations

import asyncio

import pytest
from rich.cells import cell_len
from rich.console import Console
from rich.text import Text
from textual.app import App, ComposeResult
from textual.coordinate import Coordinate
from textual.widgets import Button, Input, RichLog, Select, SelectionList

from theater.regie.trajectory.constants import (
    FILTER_MAX_ROWS,
    LEDGER_CELL_PADDING,
    LEDGER_OVERSCAN_ROWS,
    LEDGER_SPAN_ROW_HEIGHT,
    TIMELINE_HOVER_LEFT_GLYPH,
    TIMELINE_HOVER_RIGHT_GLYPH,
    TIMELINE_LABEL_RIGHT_PADDING,
    TIMELINE_LABEL_WIDTH,
    TIMELINE_LANE_HEIGHT,
    TIMELINE_RELATED_GLYPH,
    TIMELINE_SPAN_MIN_WIDTH,
    TIMELINE_TURN_BOUNDARY_GLYPH,
    TRAJECTORY_TOOLTIP_SUMMARY_MAX_CELLS,
)
from theater.regie.trajectory.details import (
    DETAIL_PARTICIPANT_META,
    build_span_details,
)
from theater.regie.trajectory.enums import FilterDimension, InspectorTab, OrderMode
from theater.regie.trajectory.filter_panel import FilterPanel
from theater.regie.trajectory.hover_card import TimelineHoverCard
from theater.regie.trajectory.ledger import (
    Ledger,
    LedgerOlderClicked,
    LedgerRecordClicked,
    LedgerRecordHovered,
    LedgerRetryClicked,
)
from theater.regie.trajectory.models import decode_delta, decode_page
from theater.regie.trajectory.ordering import build_ordering
from theater.regie.trajectory.render import (
    detail_text,
    record_line,
    sanitize_text,
    tabs_for_record,
    tooltip_text,
)
from theater.regie.trajectory.search import FilterCounts, search_records
from theater.regie.trajectory.span_detail import SpanDetailPanel
from theater.regie.trajectory.timeline import Timeline, TimelineSpanClicked, TimelineSpanHovered
from theater.regie.trajectory.timeline_layout import build_timeline_layout
from theater.regie.trajectory.view import TrajectoryParticipantSelected, TrajectoryView
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
        assert int(ledger.scroll_y) == 10 * LEDGER_SPAN_ROW_HEIGHT
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

        assert ledger.cell_padding == LEDGER_CELL_PADDING
        assert columns[Ledger.COLUMN_SOURCE].width == cell_len("long-adapter-source")
        assert columns[Ledger.COLUMN_STATUS].width == cell_len("● INTERRUPTED")
        assert columns[Ledger.COLUMN_SOURCE].get_render_width(ledger) == (
            cell_len("long-adapter-source") + 2 * LEDGER_CELL_PADDING
        )


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
        normal_style = timeline._span_style(records[-1])
        timeline.set_hovered("r9")
        hovered_style = timeline._span_style(records[-1])
        assert hovered_style.bgcolor == normal_style.bgcolor


async def test_timeline_projects_four_lanes_and_duration_widths() -> None:
    records = [
        record("input", index=0, lane="input", kind="user"),
        record("model", index=1, lane="model", kind="assistant"),
        record("tools", index=2, lane="tools", kind="tool_call"),
        record("theater", index=3, lane="theater", kind="spawn"),
    ]
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = await populate(app, records)
        timeline = view.query_one(Timeline)
        assert {span.lane for span in timeline.projection.spans} == set(TrajectoryLane)
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
            TrajectoryLane.MODEL,
            0,
            timeline.projection.width,
        )
        assert model_strip.text == " " * timeline.projection.width
        assert any(segment.style and segment.style.bgcolor for segment in model_strip._segments)

        model_top = timeline._lane_strip(
            TrajectoryLane.MODEL,
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


async def test_timeline_hover_marks_span_edges_and_related_records() -> None:
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

        assert timeline.related_ids == frozenset({"second"})
        assert view.state.related_record_ids("call") == frozenset({"result"})
        strip = timeline._lane_strip(TrajectoryLane.MODEL, 0, timeline.projection.width)
        hovered = timeline.projection.span_for("first")
        related = timeline.projection.span_for("second")
        assert hovered is not None and related is not None
        assert strip.text[hovered.visual_start] == TIMELINE_HOVER_LEFT_GLYPH
        assert strip.text[hovered.visual_end - 1] == TIMELINE_HOVER_RIGHT_GLYPH
        assert (
            strip.text[(related.visual_start + related.visual_end - 1) // 2]
            == TIMELINE_RELATED_GLYPH
        )


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
        strip = timeline._lane_strip(TrajectoryLane.MODEL, 0, timeline.projection.width)

        assert TIMELINE_LANE_HEIGHT == 2
        assert timeline.virtual_size.height == len(TrajectoryLane) * TIMELINE_LANE_HEIGHT
        assert strip.text[next_span.x] == TIMELINE_TURN_BOUNDARY_GLYPH


async def test_timeline_lane_labels_are_right_aligned() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = await populate(app, [record("model", index=0)])
        timeline = view.query_one(Timeline)
        model_middle = 1 + list(TrajectoryLane).index(TrajectoryLane.MODEL) * TIMELINE_LANE_HEIGHT

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


def test_search_cache_and_hover_path_do_not_recompute_search() -> None:
    async def scenario() -> None:
        app = Host()
        async with app.run_test(size=(100, 30)):
            view = await populate(app, [record("r1"), record("r2", index=2, turn_id=None)])
            key = view._search_key
            cache_sizes = (len(view._search_cache.corpus), len(view._search_cache.query_scores))
            view.on_ledger_record_hovered(type("Hover", (), {"record_id": "r1"})())
            assert view._search_key == key
            assert (
                len(view._search_cache.corpus),
                len(view._search_cache.query_scores),
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

        monkeypatch.setattr("theater.regie.trajectory.hover_card.tooltip_text", render_tooltip)
        lane_y = tuple(TrajectoryLane).index(item.lane) * TIMELINE_LANE_HEIGHT + 1
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
        lane_y = tuple(TrajectoryLane).index(item.lane) * TIMELINE_LANE_HEIGHT + 1

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
    assert details.tabs == (
        InspectorTab.CURRENT,
        InspectorTab.PREVIOUS,
        InspectorTab.DIFF,
    )
    assert details.tab is InspectorTab.CURRENT


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
    async with app.run_test(size=(80, 24)):
        view = await populate(app, [item])
        view._open_details("r1")
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
        retry = ledger.get_cell(Ledger.RETRY_KEY, Ledger.COLUMN_STATUS)
        assert isinstance(retry, Text)
        assert retry.plain.strip() == "↻ Retry"
        row = ledger.get_row_index(Ledger.RETRY_KEY)
        assert ledger.ordered_rows[row].height == 2
        column = ledger.get_column_index(Ledger.COLUMN_STATUS)
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
