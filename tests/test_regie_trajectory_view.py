from __future__ import annotations

from textual.app import App, ComposeResult
from textual.coordinate import Coordinate
from textual.widgets import Button, DataTable, Input, Select, SelectionList

from theater.regie.trajectory.constants import (
    LEDGER_HEADER_HEIGHT,
    LEDGER_ROW_HEIGHT,
    SEARCH_HEIGHT,
    TRAJECTORY_FOOTER_HEIGHT,
    TRAJECTORY_HORIZONTAL_PADDING,
)
from theater.regie.trajectory.enums import FocusRegion, InspectorTab
from theater.regie.trajectory.filter_panel import FilterPanel
from theater.regie.trajectory.footer import TrajectoryFooter
from theater.regie.trajectory.ledger import Ledger
from theater.regie.trajectory.state import ParticipantTrajectoryState, TrajectoryStateStore
from theater.regie.trajectory.timeline import Timeline
from theater.regie.trajectory.view import ReturnToTree, TrajectoryRetryRequested, TrajectoryView
from theater.trajectory import PanelState, PanelStateInfo, TrajectoryPage, TrajectoryRecord


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
        view.state.expanded_id = "r1"

        view.enter_live_tail()

        assert view.state.follow_tail
        assert view.state.selected_id == "r3"
        assert view.state.ledger_page == 2
        assert view.state.hovered_id is None
        assert view.state.expanded_id is None


async def test_surface_uses_fixed_timeline_and_virtualized_ledger() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = await add_records(app)

        assert isinstance(view.query_one("#trajectory-timeline"), Timeline)
        assert isinstance(view.query_one("#trajectory-ledger"), Ledger)
        assert isinstance(view.query_one("#trajectory-footer"), TrajectoryFooter)
        assert isinstance(view.query_one("#trajectory-filters"), FilterPanel)
        assert isinstance(view.query_one("#trajectory-filter-options"), SelectionList)
        assert isinstance(view.query_one("#trajectory-ledger"), DataTable)
        assert isinstance(view.query_one("#trajectory-page"), Select)
        buttons = list(view.query_one(TrajectoryFooter).query(Button))
        assert len(buttons) == 7
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
        assert all(row.height == LEDGER_ROW_HEIGHT for row in ledger.rows.values())
        assert all(
            button.region.height == 1
            for button in view.query_one(TrajectoryFooter).query(Button)
            if button.display
        )
        assert len(view.query(Ledger)) == 1
        assert len(view.query("#trajectory-inspector")) == 0
        assert view.styles.padding.left == TRAJECTORY_HORIZONTAL_PADDING
        assert view.styles.padding.right == TRAJECTORY_HORIZONTAL_PADDING


async def test_keys_route_regions_selection_search_reset_and_escape() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        view = await add_records(app)
        assert view.state.selected_id == "r2"

        await pilot.press("k")
        assert view.state.selected_id == "r1"
        await pilot.press("enter")
        assert view.state.focus_region is FocusRegion.LEDGER
        assert view.state.expanded_id == "r1"
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

        await pilot.click("#trajectory-view-action")

        assert view.state.diagnostic_view.value == "running"
        assert view.query_one(Ledger) is ledger
        assert "Running" in str(view.query_one("#trajectory-view-action", Button).label)
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
        await pilot.click(
            ledger,
            offset=(2, ledger.header_height + row * LEDGER_ROW_HEIGHT + 1),
        )
        assert view.state.selected_id == "r1"
        assert view.state.expanded_id == "r1"


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


async def test_details_expand_beneath_the_record_and_toggle_closed() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        view = await add_records(app)
        ledger = view.query_one("#trajectory-ledger", Ledger)
        await pilot.press("enter")
        record_row = ledger.get_row_index("record:r2")
        assert ledger.get_row_index("detail:r2") == record_row + 1
        assert ledger.ordered_rows[record_row].height == LEDGER_ROW_HEIGHT
        assert ledger.ordered_rows[record_row + 1].height >= 3

        event_column = ledger.get_column_index(Ledger.COLUMN_EVENT)
        detail_region = ledger._get_cell_region(Coordinate(record_row + 1, event_column))
        await pilot.click(
            ledger,
            offset=(detail_region.x + 2, detail_region.y - int(ledger.scroll_y) + 1),
        )
        assert view.state.detail_tab is InspectorTab.OUTPUT

        await pilot.press("enter")
        assert view.state.expanded_id is None
        assert "detail:r2" not in {row.key.value for row in ledger.ordered_rows}


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
