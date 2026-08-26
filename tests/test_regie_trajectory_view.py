from __future__ import annotations

from textual.app import App, ComposeResult
from textual.coordinate import Coordinate
from textual.widgets import Button, DataTable, Input, RichLog, Select, SelectionList

from theater.constants.regie_trajectory import (
    LEDGER_HEADER_HEIGHT,
    SEARCH_HEIGHT,
    TIMELINE_LANE_HEIGHT,
    TRAJECTORY_FOOTER_HEIGHT,
    TRAJECTORY_HORIZONTAL_PADDING,
    TRAJECTORY_HOVERED_SPAN_ROW_HEIGHT,
    TRAJECTORY_SPAN_ROW_HEIGHT,
)
from theater.regie.trajectory.enums import FocusRegion, InspectorTab
from theater.regie.trajectory.state import ParticipantTrajectoryState, TrajectoryStateStore
from theater.regie.trajectory.view import ReturnToTree, TrajectoryRetryRequested, TrajectoryView
from theater.regie.trajectory.widgets.filter_panel import FilterPanel
from theater.regie.trajectory.widgets.footer import TrajectoryFooter
from theater.regie.trajectory.widgets.ledger import Ledger
from theater.regie.trajectory.widgets.span_detail import SpanDetailPanel
from theater.regie.trajectory.widgets.timeline import Timeline
from theater.trajectory import (
    PanelState,
    PanelStateInfo,
    TrajectoryLane,
    TrajectoryPage,
    TrajectoryRecord,
)


def make_record(record_id: str, summary: str, *, turn_id: str | None = "t1") -> TrajectoryRecord:
    return TrajectoryRecord.from_wire(
        {
            "record_id": record_id,
            "revision": 1,
            "participant_id": "p1",
            "source_epoch": "epoch",
            "lane": "model",
            "kind": "assistant",
            "source": "claude",
            "summary": summary,
            "status": "completed",
            "raw_index": int(record_id.removeprefix("r") or 0),
            "turn_id": turn_id,
            "details": [
                {
                    "name": "output",
                    "format": "text",
                    "value": {"text": summary, "omitted_bytes": 0},
                }
            ],
        }
    )


class Host(App):
    def __init__(
        self,
        *,
        copied: list[str] | None = None,
        state_store: TrajectoryStateStore | None = None,
    ) -> None:
        super().__init__()
        self.copied = copied if copied is not None else []
        self.state_store = state_store
        self.returned = 0

    def compose(self) -> ComposeResult:
        yield TrajectoryView(
            "p1",
            copy_request=self.copied.append,
            state_store=self.state_store,
            id="trajectory",
        )

    def on_return_to_tree(self, _message: ReturnToTree) -> None:
        self.returned += 1


async def add_records(app: Host) -> TrajectoryView:
    view = app.query_one("#trajectory", TrajectoryView)
    view.state.panel = PanelStateInfo(PanelState.READY, participant_state="live")
    view.state.upsert([make_record("r1", "first"), make_record("r2", "second", turn_id=None)])
    view._refresh()
    return view


def test_snapshot_preserves_selection_only_while_tail_following_is_paused() -> None:
    state = ParticipantTrajectoryState("p1")
    first = make_record("r1", "first")
    second = make_record("r2", "second")
    third = make_record("r3", "third")
    state.apply_snapshot(TrajectoryPage(PanelStateInfo(PanelState.READY), records=(first, second)))
    state.select("r1")

    state.apply_snapshot(
        TrajectoryPage(PanelStateInfo(PanelState.READY), records=(first, second, third))
    )

    assert state.selected_id == "r3"
    state.pause_follow()
    state.select("r1")
    state.apply_snapshot(
        TrajectoryPage(PanelStateInfo(PanelState.READY), records=(first, second, third))
    )
    assert state.selected_id == "r1"


async def test_enter_live_tail_selects_final_page_and_clears_transient_details() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = app.query_one(TrajectoryView)
        view.state_store.page_size = 1
        view.state.upsert(
            [
                make_record("r1", "first"),
                make_record("r2", "second"),
                make_record("r3", "third"),
            ]
        )
        view.state.select("r1")
        view.state.pause_follow()
        view.state.hovered_id = "r1"
        view.state.detail_id = "r1"

        view.enter_live_tail()

        assert view.state.follow_tail
        assert view.state.selected_id == "r3"
        assert view.state.ledger_page == 2
        assert view.state.hovered_id is None
        assert view.state.detail_id is None


async def test_surface_uses_fixed_timeline_and_virtualized_ledger() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = await add_records(app)

        assert isinstance(view.query_one("#trajectory-timeline"), Timeline)
        assert isinstance(view.query_one("#trajectory-ledger"), Ledger)
        assert isinstance(view.query_one("#trajectory-span-detail"), SpanDetailPanel)
        assert isinstance(view.query_one("#trajectory-footer"), TrajectoryFooter)
        assert isinstance(view.query_one("#trajectory-filters"), FilterPanel)
        assert isinstance(view.query_one("#trajectory-filter-options"), SelectionList)
        assert isinstance(view.query_one("#trajectory-ledger"), DataTable)
        assert isinstance(view.query_one("#trajectory-page"), Select)
        assert isinstance(view.query_one("#trajectory-view-action"), Select)
        buttons = list(view.query_one(TrajectoryFooter).query(Button))
        assert len(buttons) == 6
        assert all(button.display for button in buttons)
        assert view.query_one(TrajectoryFooter).region.height == TRAJECTORY_FOOTER_HEIGHT
        search = view.query_one("#trajectory-search", Input)
        timeline = view.query_one(Timeline)
        ledger = view.query_one(Ledger)
        assert search.region.height == SEARCH_HEIGHT
        assert search.region.y < timeline.region.y
        assert search.region.width == timeline.region.width == view.content_region.width
        assert timeline.styles.scrollbar_size_horizontal == 0
        assert timeline.styles.scrollbar_size_vertical == 0
        assert ledger.styles.scrollbar_size_horizontal == 0
        assert ledger.styles.scrollbar_size_vertical == 0
        assert ledger.header_height == LEDGER_HEADER_HEIGHT
        assert [row.height for row in ledger.ordered_rows] == [
            TRAJECTORY_SPAN_ROW_HEIGHT,
            TRAJECTORY_HOVERED_SPAN_ROW_HEIGHT,
        ]
        assert all(
            button.region.height == 1
            for button in view.query_one(TrajectoryFooter).query(Button)
            if button.display
        )
        assert len(view.query(Ledger)) == 1
        assert len(view.query("#trajectory-inspector")) == 0
        assert view.styles.padding.left == TRAJECTORY_HORIZONTAL_PADDING
        assert view.styles.padding.right == TRAJECTORY_HORIZONTAL_PADDING


async def test_pointer_hover_expands_only_the_active_ledger_span(monkeypatch) -> None:
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        await add_records(app)
        await pilot.pause()
        ledger = app.query_one(Ledger)
        rebuilds = 0
        original_rebuild = ledger._rebuild

        def count_rebuild(*, preserve_scroll: bool = True) -> None:
            nonlocal rebuilds
            rebuilds += 1
            original_rebuild(preserve_scroll=preserve_scroll)

        monkeypatch.setattr(ledger, "_rebuild", count_rebuild)
        first_row = ledger.get_row_index("record:r1")
        first_region = ledger._get_cell_region(Coordinate(first_row, 0))
        await pilot.hover(ledger, offset=(first_region.x, first_region.y))

        assert ledger.ordered_rows[first_row].height == TRAJECTORY_HOVERED_SPAN_ROW_HEIGHT
        assert ledger.get_cell("record:r1", Ledger.COLUMN_SUMMARY).plain.startswith("\n")
        assert not ledger.get_cell("record:r1", Ledger.COLUMN_SUMMARY).plain.startswith("\n\n")

        ledger.set_hovered("r2")
        assert ledger.ordered_rows[first_row].height == TRAJECTORY_HOVERED_SPAN_ROW_HEIGHT
        assert (
            ledger.ordered_rows[ledger.get_row_index("record:r2")].height
            == TRAJECTORY_SPAN_ROW_HEIGHT
        )

        second_row = ledger.get_row_index("record:r2")
        second_region = ledger._get_cell_region(Coordinate(second_row, 0))
        await pilot.hover(ledger, offset=(second_region.x, second_region.y))

        assert ledger.ordered_rows[first_row].height == TRAJECTORY_SPAN_ROW_HEIGHT
        assert ledger.ordered_rows[second_row].height == TRAJECTORY_HOVERED_SPAN_ROW_HEIGHT
        assert not ledger.get_cell("record:r1", Ledger.COLUMN_SUMMARY).plain.startswith("\n")
        assert ledger.get_cell("record:r2", Ledger.COLUMN_SUMMARY).plain.startswith("\n")
        assert not ledger.get_cell("record:r2", Ledger.COLUMN_SUMMARY).plain.startswith("\n\n")

        await pilot.hover("#trajectory-search")
        assert [row.height for row in ledger.ordered_rows] == [
            TRAJECTORY_SPAN_ROW_HEIGHT,
            TRAJECTORY_HOVERED_SPAN_ROW_HEIGHT,
        ]

        await pilot.press("k")
        assert [row.height for row in ledger.ordered_rows] == [
            TRAJECTORY_HOVERED_SPAN_ROW_HEIGHT,
            TRAJECTORY_SPAN_ROW_HEIGHT,
        ]
        assert rebuilds == 0


async def test_keys_route_regions_selection_search_reset_and_escape() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        view = await add_records(app)
        assert view.state.selected_id == "r2"

        await pilot.press("k")
        assert view.state.selected_id == "r1"
        await pilot.press("enter")
        assert view.state.focus_region is FocusRegion.DETAIL
        assert view.state.detail_id == "r1"
        old_tab = view.state.detail_tab
        await pilot.press("l")
        assert view.state.detail_tab != old_tab
        await pilot.press("/")
        await pilot.press(*"second")
        assert view.state.query == "second"
        view.action_reset()
        assert view.state.query == ""
        view.focus_region(FocusRegion.LEDGER)
        await pilot.press("escape")
        assert app.returned == 1


async def test_diagnostic_view_action_updates_in_place() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        view = app.query_one("#trajectory", TrajectoryView)
        view.state.panel = PanelStateInfo(PanelState.READY, participant_state="live")
        view.state.upsert([make_record("r1", "running")])
        view.state.upsert(
            [
                TrajectoryRecord.from_wire(
                    {**view.state.records["r1"].to_wire(), "status": "running", "revision": 2}
                )
            ]
        )
        view._refresh()
        ledger = view.query_one(Ledger)

        selector = view.query_one("#trajectory-view-action", Select)
        selector.value = "running"
        await pilot.pause()

        assert view.state.diagnostic_view.value == "running"
        assert view.query_one(Ledger) is ledger
        assert selector.value == "running"
        await pilot.press("v")
        assert view.state.diagnostic_view.value == "errors"


async def test_native_controls_handle_mouse_search_filters_and_row_activation() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        view = await add_records(app)
        await pilot.pause()

        await pilot.click("#trajectory-mode-action")
        assert view.state.order_mode.value == "duration"

        await pilot.click("#trajectory-filter-action")
        filters = app.query_one("#trajectory-filter-options", SelectionList)
        assert view.state.filters_open
        assert app.focused is filters
        await pilot.press("space")
        assert view.state.lane_filters
        await pilot.click("#trajectory-filter-clear")
        assert not view.state.lane_filters
        await pilot.click("#trajectory-filter-done")
        assert not view.state.filters_open

        await pilot.click("#trajectory-search", offset=(3, 1))
        await pilot.press(*"first")
        assert view.state.query == "first"
        await pilot.press("left", "delete")
        assert view.state.query == "firs"
        await pilot.press("backspace")
        assert view.state.query == "fir"
        await pilot.press("escape")
        assert not view.state.search_open

        ledger = app.query_one(Ledger)
        row = ledger.get_row_index("record:r1")
        for column in range(len(ledger.ordered_columns)):
            region = ledger._get_cell_region(Coordinate(row, column))
            await pilot.click(
                ledger,
                offset=(region.x + max(0, region.width - 2), region.y),
            )
            assert view.state.selected_id == "r1"
            assert view.state.detail_id == "r1"
            view._close_details()
            await pilot.pause()


async def test_transcript_failure_shows_retry_inside_the_error_row() -> None:
    class RetryHost(Host):
        def __init__(self) -> None:
            super().__init__()
            self.retries = 0

        def on_trajectory_retry_requested(self, _message: TrajectoryRetryRequested) -> None:
            self.retries += 1

    app = RetryHost()
    async with app.run_test(size=(100, 30)) as pilot:
        view = await add_records(app)
        view.state.mark_stale("transcript identity was lost")
        view._refresh()
        await pilot.pause()
        ledger = app.query_one(Ledger)
        retry = ledger.get_cell(Ledger.RETRY_KEY, Ledger.COLUMN_STATUS)

        assert "Retry" in retry.plain
        assert not app.query("#trajectory-retry-action")
        row = ledger.get_row_index(Ledger.RETRY_KEY)
        column = ledger.get_column_index(Ledger.COLUMN_STATUS)
        region = ledger._get_cell_region(Coordinate(row, column))
        await pilot.click(
            ledger,
            offset=(region.x + 2, region.y - int(ledger.scroll_y) + 1),
        )
        await pilot.pause()

        assert app.retries == 1


async def test_copy_is_injected_and_literal_data_is_not_rich_escaped() -> None:
    copied: list[str] = []
    app = Host(copied=copied)
    async with app.run_test(size=(100, 30)) as pilot:
        view = await add_records(app)
        view.state.records["r2"] = make_record("r2", "second [literal] \\ path", turn_id=None)
        view._refresh()
        await pilot.press("y")
        await pilot.pause()

        assert copied
        assert "second" in copied[0]
        assert "[literal] \\ path" in copied[0]


async def test_details_replace_only_the_ledger_and_close_back_to_the_list() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        view = await add_records(app)
        ledger = view.query_one("#trajectory-ledger", Ledger)
        panel = view.query_one("#trajectory-span-detail", SpanDetailPanel)
        ledger_region = ledger.region
        search_region = view.query_one("#trajectory-search", Input).region
        timeline_region = view.query_one(Timeline).region
        footer_region = view.query_one(TrajectoryFooter).region

        await pilot.press("enter")
        await pilot.pause()

        assert view.state.detail_id == "r2"
        assert ledger.has_class("-hidden")
        assert not panel.has_class("-hidden")
        assert panel.region == ledger_region
        assert view.query_one("#trajectory-search", Input).region == search_region
        assert view.query_one(Timeline).region == timeline_region
        assert view.query_one(TrajectoryFooter).region == footer_region
        assert panel.copy_text.endswith("second")
        assert panel.query_one("#trajectory-span-detail-content-summary", RichLog).lines
        assert not panel.query("#trajectory-span-detail-maximize")
        assert not panel.query("#trajectory-span-detail-resize")

        await pilot.press("l")
        assert view.state.detail_tab is InspectorTab.OUTPUT

        await pilot.click("#trajectory-span-detail-close")
        assert view.state.detail_id is None
        assert not ledger.has_class("-hidden")
        assert panel.has_class("-hidden")
        assert app.focused is ledger


async def test_timeline_click_replaces_the_open_span_detail() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        view = await add_records(app)
        view._open_details("r1")
        panel = view.query_one(SpanDetailPanel)
        timeline = view.query_one(Timeline)

        assert panel.record_id == "r1"
        assert panel.copy_text.endswith("first")

        record = view.state.records["r2"]
        anchor = timeline.hover_anchor(record.record_id)
        assert anchor is not None
        lane_y = tuple(TrajectoryLane).index(record.lane) * TIMELINE_LANE_HEIGHT + 1
        await pilot.click(
            timeline,
            offset=(anchor.x - timeline.region.x, lane_y),
        )
        await pilot.pause()

        assert view.state.detail_id == "r2"
        assert panel.record_id == "r2"
        assert panel.copy_text.endswith("second")
        assert not panel.has_class("-hidden")
        assert view.query_one(Ledger).has_class("-hidden")


async def test_span_detail_keeps_full_bounded_content_scrollable() -> None:
    app = Host()
    async with app.run_test(size=(80, 24)) as pilot:
        view = app.query_one(TrajectoryView)
        long_text = "\n".join(f"line {index}" for index in range(100))
        view.state.upsert([make_record("r1", long_text, turn_id=None)])
        view._refresh()

        await pilot.press("enter")
        await pilot.pause()

        panel = view.query_one(SpanDetailPanel)
        log = panel.query_one("#trajectory-span-detail-content-summary", RichLog)
        assert "line 99" in panel.copy_text
        assert log.virtual_size.height > log.scrollable_content_region.height


async def test_escape_closes_span_before_returning_to_tree() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        view = await add_records(app)
        await pilot.press("enter", "escape")

        assert view.state.detail_id is None
        assert app.returned == 0

        await pilot.press("escape")
        assert app.returned == 1


async def test_remount_restores_the_participant_search_state() -> None:
    states = TrajectoryStateStore()
    state = states.get("p1")
    state.search_open = True
    state.query = "saved query"
    app = Host(state_store=states)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        view = app.query_one("#trajectory", TrajectoryView)
        search = app.query_one("#trajectory-search", Input)
        assert view.state is state
        assert view.state.search_open
        assert not search.has_class("-hidden")
        assert search.value == "saved query"
