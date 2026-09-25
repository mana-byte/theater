from __future__ import annotations

import pytest
from regie.trajectory.rich.enums import FocusRegion
from regie.trajectory.rich.search import filter_matching_records
from regie.trajectory.rich.state import ParticipantTrajectoryState, TrajectoryStateStore
from regie.trajectory.rich.view import ReturnToTree, TrajectoryView
from regie.trajectory.rich.widgets.footer import TrajectoryFooter
from regie.trajectory.rich.widgets.span_detail import SpanDetailPanel
from regie.trajectory.rich.widgets.timeline import Timeline
from regie.widgets.prompts import ControlPromptScreen
from textual.app import App, ComposeResult
from textual.widgets import Input

from tests.rig.waiting import wait_until
from theater.frontend.trajectory import PanelState, PanelStateInfo, TrajectoryPage, TrajectoryRecord


def make_record(
    record_id: str, summary: str, *, turn_id: str | None = "t1", lane: str = "model"
) -> TrajectoryRecord:
    return TrajectoryRecord.from_wire(
        {
            "record_id": record_id,
            "revision": 1,
            "participant_id": "p1",
            "source_epoch": "epoch",
            "lane": lane,
            "kind": "assistant" if lane == "model" else "tool_call",
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


def test_filter_matching_records_preserves_order_and_blank_queries() -> None:
    records = (make_record("r1", "needle"), make_record("r2", "haystack"))

    assert filter_matching_records(records, "needle") == (records[0],)
    assert filter_matching_records(records, "  ") == records


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
        assert search.styles.visibility == "visible"
        assert search.offset.y == 0
        assert search.value == "saved query"


@pytest.mark.parametrize("target", ["region", "search"])
async def test_trajectory_focus_does_not_steal_focus_from_a_modal(target: str) -> None:
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        view = await add_records(app)
        await pilot.pause()
        await app.push_screen(ControlPromptScreen("Message", "Prompt"))
        await pilot.pause()
        modal_input = app.focused
        assert isinstance(modal_input, Input)

        if target == "region":
            view.focus_region(FocusRegion.DETAIL)
        else:
            view.action_open_search()
        await pilot.pause()

        assert app.focused is modal_input


async def test_vim_keys_navigate_spans_lanes_and_details() -> None:
    app = Host()
    async with app.run_test(size=(120, 40)) as pilot:
        view = app.query_one(TrajectoryView)
        view.state.upsert(
            [
                make_record("r1", "first"),
                make_record("r2", "tool", lane="tools"),
                make_record("r3", "third"),
            ]
        )
        view._refresh()
        view.focus_region(FocusRegion.TIMELINE)
        assert view.state.selected_id == "r3"

        await pilot.press("h")
        assert view.state.selected_id == "r1"  # h stays in the model lane, skipping r2
        assert not view.state.follow_tail
        await pilot.press("j")  # the tools lane is below model
        assert view.state.selected_id == "r2"
        await pilot.press("H")
        assert view.state.selected_id == "r1"
        await pilot.press("L")
        assert (view.state.selected_id, view.state.follow_tail) == ("r3", True)
        await pilot.press("minus")
        assert view.state.timeline_zoom == 0.5

        await pilot.pause()
        assert view.has_class("-timeline-focus")  # the timeline's header shows focus
        await pilot.press("enter")
        await pilot.pause()
        assert view.state.focus_region is FocusRegion.DETAIL
        assert view.query_one(SpanDetailPanel).record_id == "r3"
        assert not view.has_class("-timeline-focus")
        await pilot.press("escape")
        await pilot.pause()
        assert view.state.focus_region is FocusRegion.TIMELINE
        assert view.has_class("-timeline-focus")
        await pilot.press("escape")
        assert app.returned == 1


async def test_search_jumps_between_matching_spans() -> None:
    app = Host()
    async with app.run_test(size=(120, 40)) as pilot:
        view = await add_records(app)
        view.focus_region(FocusRegion.TIMELINE)

        await pilot.press("slash", *"first", "enter")
        await pilot.pause()
        assert view.state.query == "first"
        assert view.state.selected_id == "r1"
        assert view.state.focus_region is FocusRegion.TIMELINE
        await pilot.press("n")
        assert view.state.selected_id == "r1"  # the only match wraps onto itself


async def test_filter_key_hides_non_matches_and_projects_live_appends() -> None:
    app = Host()
    async with app.run_test(size=(120, 40)) as pilot:
        view = await add_records(app)
        view.focus_region(FocusRegion.TIMELINE)

        await pilot.press("slash", *"first", "enter", "f")
        await wait_until(pilot, lambda: view.query_one(Timeline).span_ids == ("r1",))

        assert view.state.selected_id == "r1"
        assert "filtered: 1 of 2" in view.query_one(TrajectoryFooter).render().plain

        view.state.upsert(
            [
                make_record("r3", "another miss", turn_id=None),
                make_record("r4", "first live match", turn_id=None),
            ]
        )
        view._refresh()
        await wait_until(pilot, lambda: view.query_one(Timeline).span_ids == ("r1", "r4"))

        await pilot.press("f")
        await wait_until(
            pilot, lambda: view.query_one(Timeline).span_ids == ("r1", "r2", "r3", "r4")
        )


async def test_tool_operations_are_one_span_so_every_step_is_visible() -> None:
    app = Host()
    async with app.run_test(size=(120, 40)) as pilot:
        view = app.query_one(TrajectoryView)
        call, result = (
            TrajectoryRecord.from_wire(
                {
                    **make_record(record_id, "tool", lane="tools").to_wire(),
                    "kind": kind,
                    "call_id": "c1",
                }
            )
            for record_id, kind in (("r2", "tool_call"), ("r3", "tool_result"))
        )
        view.state.upsert([make_record("r1", "first"), call, result, make_record("r4", "last")])
        view._refresh()
        view.focus_region(FocusRegion.TIMELINE)

        assert view.query_one(Timeline).span_ids == ("r1", "r2", "r4")
        await pilot.press("j")
        assert view.state.selected_id == "r2"  # the result shares its call's span


async def test_details_wait_for_the_cursor_to_rest_and_show_loading_meanwhile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A long rest window, so a loaded CI machine cannot overrun it mid-assertion.
    monkeypatch.setattr("regie.trajectory.rich.view.TRAJECTORY_DETAIL_SETTLE_SECONDS", 1.0)
    app = Host()
    async with app.run_test(size=(120, 40)) as pilot:
        view = await add_records(app)
        view.focus_region(FocusRegion.TIMELINE)
        panel = view.query_one(SpanDetailPanel)
        loading = panel.query_one("#trajectory-span-detail-loading")
        await wait_until(pilot, lambda: panel.record_id == "r2" and not loading.display)

        await pilot.press("h")
        assert panel.record_id == "r2" and loading.display  # still moving: not loaded yet
        await wait_until(pilot, lambda: panel.record_id == "r1" and not loading.display)


async def test_detail_keys_move_between_sections_and_copy_section_or_page() -> None:
    copied: list[str] = []
    app = Host(copied=copied)
    async with app.run_test(size=(120, 40)) as pilot:
        await add_records(app)
        await pilot.press("enter")
        await pilot.pause()
        panel = app.query_one(SpanDetailPanel)
        assert panel.selected_section is not None
        assert panel.selected_section.title == "Output"

        await pilot.press("y", "l")  # h/l scroll; they do not move between sections
        await pilot.pause()
        assert panel.selected_section.title == "Output"
        await pilot.press("j")
        await pilot.pause()
        assert panel.selected_section.title == "Debug"
        await pilot.press("Y")
        await pilot.pause()
        assert copied[0] == "second"
        assert copied[1].startswith("## Output") and "## Debug" in copied[1]


async def test_empty_spans_stay_off_the_timeline() -> None:
    app = Host()
    async with app.run_test(size=(120, 40)):
        view = app.query_one(TrajectoryView)
        empty = TrajectoryRecord.from_wire(
            {**make_record("r2", "").to_wire(), "kind": "reasoning", "details": []}
        )
        view.state.upsert([make_record("r1", "first"), empty, make_record("r3", "third")])
        view._refresh()

        assert view.query_one(Timeline).span_ids == ("r1", "r3")
        assert not view.select_and_reveal_record("r2")


async def test_footer_key_hints_are_clickable() -> None:
    app = Host()
    async with app.run_test(size=(140, 40)) as pilot:
        view = await add_records(app)
        view.focus_region(FocusRegion.TIMELINE)
        await pilot.pause()
        footer = view.query_one(TrajectoryFooter)
        text = footer.render().plain

        await pilot.click(footer, offset=(text.index("details") + 3, 0))
        await pilot.pause()

        assert view.state.focus_region is FocusRegion.DETAIL


async def test_shift_j_and_k_move_focus_between_timeline_and_details() -> None:
    app = Host()
    async with app.run_test(size=(120, 40)) as pilot:
        view = await add_records(app)
        view.focus_region(FocusRegion.TIMELINE)

        await pilot.press("J")
        assert view.state.focus_region is FocusRegion.DETAIL
        await pilot.press("J")  # already there
        assert view.state.focus_region is FocusRegion.DETAIL
        await pilot.press("K")
        assert view.state.focus_region is FocusRegion.TIMELINE
