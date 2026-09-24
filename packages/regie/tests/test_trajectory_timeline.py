from __future__ import annotations

from regie.trajectory.domain import Timing, TimingProvenance, TrajectoryRecord
from regie.trajectory.rich.enums import TimelineLane
from regie.trajectory.rich.render.timeline import build_timeline_layout
from regie.trajectory.rich.widgets.timeline import Timeline
from regie.trajectory.ui_constants import (
    TIMELINE_GLYPH_END,
    TIMELINE_GLYPH_POINT,
    TIMELINE_GLYPH_START,
    TIMELINE_IDLE_GAP_CELLS,
    TIMELINE_LABEL_WIDTH,
    TIMELINE_TARGET_SPAN_CELLS,
    TIMELINE_ZOOM_MIN,
)
from textual.app import App, ComposeResult


def _record(
    record_id: str,
    lane: str,
    index: int,
    *,
    start: float | None = None,
    duration: float | None = None,
    mcp: bool = False,
) -> TrajectoryRecord:
    wire: dict[str, object] = {
        "record_id": record_id,
        "revision": 1,
        "participant_id": "p1",
        "source_epoch": "epoch",
        "lane": lane,
        "kind": "assistant" if lane == "model" else "tool_call",
        "source": "claude",
        "summary": record_id,
        "status": "completed",
        "raw_index": index,
    }
    if start is not None:
        timing: dict[str, object] = {"start": start, "provenance": "source"}
        if duration is not None:
            timing["end"] = start + duration
        wire["timing"] = timing
    if mcp:
        wire["mcp_server"] = "server"
        wire["mcp_tool"] = "tool"
    return TrajectoryRecord.from_wire(wire)


def test_span_widths_follow_duration_and_idle_time_collapses() -> None:
    records = (
        _record("short", "model", 1, start=0, duration=2),
        _record("long", "tools", 2, start=2, duration=8),
        _record("instant", "model", 3, start=10),
        _record("untimed", "model", 4),
        _record("later", "model", 5, start=1_000, duration=2),
    )
    layout = build_timeline_layout(records, minimum_width=60, zoom=TIMELINE_ZOOM_MIN)
    spans = {span.record_id: span for span in layout.spans}

    assert spans["long"].width > 3 * spans["short"].width  # 8s against 2s, give or take caps
    assert spans["instant"].point and spans["instant"].width == 1
    assert spans["untimed"].x == spans["instant"].x  # untimed records sit at the prior time
    assert spans["later"].x - spans["instant"].x <= TIMELINE_IDLE_GAP_CELLS
    assert [span.x for span in layout.spans] == sorted(span.x for span in layout.spans)
    assert layout.width == 60


def test_default_zoom_keeps_spans_readable_and_zooming_out_stops_at_fit() -> None:
    records = tuple(
        _record(f"r{index}", "model", index, start=index * 10, duration=10) for index in range(50)
    )
    readable = build_timeline_layout(records, minimum_width=80)
    fitted = build_timeline_layout(records, minimum_width=80, zoom=TIMELINE_ZOOM_MIN)

    assert {span.width for span in readable.spans} == {TIMELINE_TARGET_SPAN_CELLS}
    assert readable.width > 80  # wider than the screen; the timeline scrolls
    assert fitted.width == 80


def test_split_records_take_their_operation_interval() -> None:
    call = _record("call", "tools", 1, start=0)
    timing = Timing(start=0, end=10, provenance=TimingProvenance.SOURCE)
    alone = build_timeline_layout((call,), minimum_width=40).spans[0]
    derived = build_timeline_layout((call,), minimum_width=40, timing_for=lambda _id: timing).spans[
        0
    ]

    assert alone.point
    assert not derived.point and derived.width > 1


class _Host(App):
    def compose(self) -> ComposeResult:
        yield Timeline(id="timeline")


async def test_bars_have_caps_and_clicks_hit_the_span_under_the_pointer() -> None:
    records = [
        _record("model", "model", 1, start=0, duration=5),
        _record("mcp", "tools", 2, start=5, mcp=True),
    ]
    async with _Host().run_test(size=(80, 20)) as pilot:
        timeline = pilot.app.query_one(Timeline)
        timeline.update_records(records)
        await pilot.pause()
        model = timeline.projection.span_for("model")
        mcp = timeline.projection.span_for("mcp")
        assert model is not None and mcp is not None and mcp.lane is TimelineLane.MCP

        bar = timeline._lane_strip(TimelineLane.MODEL, 0, timeline.projection.width).text
        assert bar[model.x] == TIMELINE_GLYPH_START
        assert bar[model.end - 1] == TIMELINE_GLYPH_END
        point = timeline._lane_strip(TimelineLane.MCP, 0, timeline.projection.width).text
        assert point[mcp.x] == TIMELINE_GLYPH_POINT

        lane_colors = {lane: timeline._component(lane.value).color for lane in TimelineLane}
        assert len(set(lane_colors.values())) == len(TimelineLane)  # each lane is distinct

        bar_row = timeline.track_y(TimelineLane.MODEL)
        assert timeline._record_at(TIMELINE_LABEL_WIDTH + model.x + 1, bar_row) == records[0]


async def test_j_k_pick_a_lane_and_h_l_stay_inside_it() -> None:
    records = [
        _record(record_id, "model" if record_id[0] == "m" else "tools", index, start=index)
        for index, record_id in enumerate(("m1", "t2", "m3", "m4", "t5"), start=1)
    ]
    async with _Host().run_test(size=(120, 30)) as pilot:
        timeline = pilot.app.query_one(Timeline)
        timeline.update_records(records, selected_id="m4")
        await pilot.pause()

        assert timeline.move_span(-1) == "m3"  # skips t2: tools is another lane
        assert timeline.move_span(-1) == "m1"
        assert timeline.move_span(-1) == "m1"  # stops at the lane's first span
        assert timeline.move_lane(1) == "t2"  # nearest tools span to m1
        assert timeline.move_span(1) == "t5"
        assert timeline.move_span(1) == "t5"
        assert timeline.move_lane(1) == "t5"  # no populated lane below tools
        assert timeline.move_lane(-1) == "m4"


def test_concurrent_spans_stack_into_rows_of_their_lane() -> None:
    records = (
        _record("a", "tools", 1, start=0, duration=10, mcp=True),
        _record("b", "tools", 2, start=2, duration=4, mcp=True),
        _record("c", "tools", 3, start=3, duration=1, mcp=True),
        _record("d", "tools", 4, start=11, duration=2, mcp=True),
    )
    layout = build_timeline_layout(records, minimum_width=80)
    rows = {span.record_id: span.row for span in layout.spans}

    assert rows == {"a": 0, "b": 1, "c": 2, "d": 0}  # d starts after a ends, so it reuses row 0
    assert layout.rows_for(TimelineLane.MCP) == 3
    assert layout.rows_for(TimelineLane.MODEL) == 1


async def test_selected_span_is_centered_unless_near_an_edge() -> None:
    records = [
        _record(f"m{index}", "model", index, start=index * 10, duration=10) for index in range(40)
    ]
    async with _Host().run_test(size=(80, 20)) as pilot:
        timeline = pilot.app.query_one(Timeline)
        timeline.update_records(records)
        await pilot.pause()
        cells = timeline._available_cells()

        timeline._select_span("m20")
        middle = timeline.projection.span_for("m20")
        assert middle is not None
        center = (middle.x + middle.end) // 2 - timeline.horizontal_offset
        assert abs(center - cells // 2) <= 1
        timeline._select_span("m0")
        assert timeline.horizontal_offset == 0
        timeline._select_span("m39")
        assert timeline.horizontal_offset == timeline.tail_offset
