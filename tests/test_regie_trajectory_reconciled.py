from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from rich.console import Console
from textual.app import App, ComposeResult
from textual.widgets import Input

from theater.regie.trajectory.constants import (
    FILTER_MAX_ROWS,
    LEDGER_OVERSCAN_ROWS,
    TIMELINE_PADDING,
)
from theater.regie.trajectory.enums import FilterDimension, InspectorTab
from theater.regie.trajectory.filter_panel import FilterPanel
from theater.regie.trajectory.inspector import (
    Inspector,
    InspectorParticipantLinkClicked,
    InspectorResizeRequested,
)
from theater.regie.trajectory.ledger import Ledger, LedgerRetryClicked
from theater.regie.trajectory.models import decode_delta, decode_page
from theater.regie.trajectory.ordering import build_ordering
from theater.regie.trajectory.render import (
    inspector_content,
    inspector_text,
    record_line,
    sanitize_text,
    tabs_for_record,
)
from theater.regie.trajectory.search import FilterCounts, search_records
from theater.regie.trajectory.timeline import Timeline, TimelineSpanClicked
from theater.regie.trajectory.view import TrajectoryParticipantSelected, TrajectoryView
from theater.trajectory import (
    ContentFormat,
    DetailField,
    GroupKind,
    PanelState,
    PanelStateInfo,
    Timing,
    TimingProvenance,
    TrajectoryGroup,
    TrajectoryLane,
    TrajectoryRecord,
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
    timing: dict[str, object] | None = None,
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
    if timing is not None:
        result["timing"] = timing
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
    async with app.run_test(size=(100, 30)):
        await populate(app, records)
        ledger = app.query_one(Ledger)
        ledger._viewport_height = 5
        ledger.update_rows(records, search_records(records))

        assert ledger.rendered_record_count <= 5 + 2 * LEDGER_OVERSCAN_ROWS
        ledger.set_scroll_offset(10)
        assert ledger._entry_at(1).record_id == "r5"
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


async def test_timeline_scroll_hit_testing_selection_and_one_cell_per_record() -> None:
    records = [record(f"r{index}", index=index) for index in range(10)]
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = await populate(app, records)
        timeline = view.query_one(Timeline)
        timeline._viewport_width = 4
        timeline.set_scroll_offset(5)

        assert timeline._record_at(TIMELINE_PADDING).record_id == "r5"
        assert timeline.scroll_span_into_view("r9") == 8
        assert len(timeline._render_timeline().plain) == len(records)
        timeline._hovered_id = "r9"
        assert "reverse" in str(timeline._render_timeline().get_style_at_offset(Console(), 9))


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
        view.state.select("derived")
        view.action_toggle_mode()
        ledger_text = app.query_one(Ledger).render().plain

        derived_line = next(line for line in ledger_text.splitlines() if "derived timing" in line)
        missing_line = next(line for line in ledger_text.splitlines() if "missing timing" in line)
        source_line = next(line for line in ledger_text.splitlines() if "source timing" in line)
        assert "dur" not in missing_line
        assert "dur" not in derived_line
        assert "dur" in source_line
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
        assert "tools (1)" in panel.render().plain
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
        view.state.resume_follow()
        view.action_timeline_previous()
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
    current = inspector_text(item, InspectorTab.CURRENT)
    previous = inspector_text(item, InspectorTab.PREVIOUS)
    diff = inspector_text(item, InspectorTab.DIFF)
    assert '"a": [' in current and "No current" not in current
    assert "[old] \\ path" in previous and "No previous" not in previous
    assert "--- old" in diff and "No diff" not in diff
    assert inspector_content(item, InspectorTab.CURRENT).plain == current


class InspectorHost(App):
    def __init__(self, item: TrajectoryRecord) -> None:
        super().__init__()
        self.item = item
        self.links: list[str] = []
        self.resize: list[float] = []

    def compose(self) -> ComposeResult:
        yield Inspector(self.item)

    def on_inspector_participant_link_clicked(
        self, message: InspectorParticipantLinkClicked
    ) -> None:
        self.links.append(message.participant_id)

    def on_inspector_resize_requested(self, message: InspectorResizeRequested) -> None:
        self.resize.append(message.delta)


@pytest.mark.asyncio
async def test_inspector_maximize_survives_refresh_ratio_and_wheel_scroll() -> None:
    detail = DetailField.from_text("output", "line\n" * 1000, format=ContentFormat.TEXT)
    item = TrajectoryRecord(
        record_id="r1",
        revision=1,
        participant_id="p1",
        source_epoch="epoch",
        lane="model",
        kind="assistant",
        source="claude",
        summary="summary",
        status="completed",
        details=(detail,),
    )
    app = InspectorHost(item)
    async with app.run_test(size=(80, 20)) as pilot:
        inspector = app.query_one(Inspector)
        inspector.set_ratio(0.60)
        inspector.toggle_maximize()
        inspector.set_ratio(0.25)
        inspector.set_record(item, tab=InspectorTab.SUMMARY)
        inspector.resize_by(0.10)
        assert inspector.maximized
        assert str(inspector.styles.height) == "1fr"
        inspector.on_mouse_scroll_down(SimpleNamespace(shift=False, stop=lambda: None))
        await pilot.pause()
        assert inspector.maximized
        inspector._resizing = True
        inspector.on_mouse_move(SimpleNamespace(delta_y=2, stop=lambda: None))
        await pilot.pause()
        assert app.resize and app.resize[-1] < 0


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
        panel.focus()
        await pilot.press(*(["j"] * (len(panel.options) - 1)))
        assert panel._cursor == len(panel.options) - 1
        assert panel._scroll_offset > 0
        assert panel._scroll_offset <= panel._cursor < panel._scroll_offset + FILTER_MAX_ROWS
        assert any(span.style and span.style.reverse for span in panel.render().spans)


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
    app = InspectorHost(item)
    async with app.run_test(size=(80, 20)) as pilot:
        inspector = app.query_one(Inspector)
        short_line = next(line for line, value in inspector._link_line_ids.items() if value == "p")
        inspector.on_click(SimpleNamespace(y=short_line, stop=lambda: None))
        await pilot.pause()
        assert app.links == ["p"]

    called: list[str] = []

    class LinkViewHost(App):
        def compose(self) -> ComposeResult:
            yield TrajectoryView("p1", participant_link=called.append)

        def on_trajectory_participant_selected(
            self, _message: TrajectoryParticipantSelected
        ) -> None:
            called.append("fallback")

    link_app = LinkViewHost()
    async with link_app.run_test(size=(80, 20)) as pilot:
        view = link_app.query_one(TrajectoryView)
        view.on_inspector_participant_link_clicked(InspectorParticipantLinkClicked("p"))
        await pilot.pause()
    assert called == ["p"]


@pytest.mark.asyncio
async def test_retry_row_is_clickable() -> None:
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
        ledger.on_click(SimpleNamespace(y=0, stop=lambda: None))
        await pilot.pause()
        assert app.retries == 1


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
