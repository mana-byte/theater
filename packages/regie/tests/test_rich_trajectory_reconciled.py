from __future__ import annotations

import pytest
from regie.trajectory.domain import (
    GroupKind,
    PanelState,
    PanelStateInfo,
    Timing,
    TimingProvenance,
    TrajectoryGroup,
    TrajectoryLane,
    TrajectoryRecord,
)
from regie.trajectory.rich.enums import InspectorTab, OrderMode, TimelineLane
from regie.trajectory.rich.inspection.links import DETAIL_PARTICIPANT_META
from regie.trajectory.rich.inspection.project import detail_text, tabs_for_record
from regie.trajectory.rich.inspection.styled import build_span_details
from regie.trajectory.rich.models import decode_delta, decode_page
from regie.trajectory.rich.render.ordering import build_ordering
from regie.trajectory.rich.render.timeline import build_timeline_layout
from regie.trajectory.rich.view import TrajectoryParticipantSelected, TrajectoryView
from regie.trajectory.rich.widgets.span_detail import SpanDetailPanel
from regie.trajectory.rich.widgets.timeline import (
    Timeline,
    TimelineSpanClicked,
)
from regie.trajectory.ui_constants import (
    TIMELINE_LABEL_RIGHT_PADDING,
    TIMELINE_LABEL_WIDTH,
    TIMELINE_SPAN_MIN_WIDTH,
    TIMELINE_TURN_BOUNDARY_GLYPH,
)
from rich.console import Console
from textual.app import App, ComposeResult
from textual.widgets import Input, RichLog


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

        model_middle = 1 + list(TrajectoryLane).index(TrajectoryLane.MODEL) * timeline.lane_height
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
            middle = 1 + lane_index * timeline.lane_height
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

        timeline.set_hovered("first")

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
        model_middle = 1 + list(TrajectoryLane).index(TrajectoryLane.MODEL) * timeline.lane_height
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
        view._refresh()

        assert refreshes == 1


async def test_timeline_lanes_fill_their_rows_and_mark_new_turns() -> None:
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

        assert timeline.virtual_size.height == len(TimelineLane) * timeline.lane_height
        assert strip.text[next_span.x] == TIMELINE_TURN_BOUNDARY_GLYPH


async def test_timeline_lane_labels_are_right_aligned() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = await populate(app, [record("model", index=0)])
        timeline = view.query_one(Timeline)
        model_middle = 1 + list(TimelineLane).index(TimelineLane.MODEL) * timeline.lane_height

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


async def test_clicks_and_movement_pause_tail_but_hover_does_not() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = await populate(app, [record("r1"), record("r2", index=2, turn_id=None)])
        view.query_one(Timeline).set_hovered("r1")
        assert view.state.follow_tail
        view.on_timeline_span_clicked(TimelineSpanClicked("r1"))
        assert not view.state.follow_tail
        view.action_move_span(1)
        assert view.state.follow_tail  # reaching the last span follows the live tail again
        view.action_move_span(-1)
        assert not view.state.follow_tail


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
        view.select_and_reveal_record("r1")
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

        view.select_and_reveal_record("r2")
        await pilot.pause()
        assert float(log.scroll_y) == 0


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
        view.select_and_reveal_record("system")
        await pilot.pause()
        log = view.query_one(
            "#trajectory-span-detail-content-current",
            RichLog,
        )
        await pilot.click(log, offset=(3, 4))
        await pilot.pause()
    assert called == ["p"]


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
