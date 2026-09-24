from __future__ import annotations

import pytest
from regie.trajectory.domain import (
    PanelState,
    PanelStateInfo,
    TrajectoryPage,
    TrajectoryRecord,
)
from regie.trajectory.rich.enums import FocusRegion, InspectorTab
from regie.trajectory.rich.state import ParticipantTrajectoryState, TrajectoryStateStore
from regie.trajectory.rich.view import ReturnToTree, TrajectoryView
from regie.trajectory.rich.widgets.span_detail import SpanDetailPanel
from regie.trajectory.rich.widgets.timeline import Timeline
from regie.widgets.prompts import ControlPromptScreen
from textual.app import App, ComposeResult
from textual.widgets import Button, Input, RichLog


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


async def test_span_detail_copy_button_copies_active_tab() -> None:
    copied: list[str] = []
    app = Host(copied=copied)
    async with app.run_test(size=(100, 30)) as pilot:
        view = await add_records(app)
        record = view.state.records["r2"]
        view.state.upsert(
            [
                TrajectoryRecord.from_wire(
                    {
                        **record.to_wire(),
                        "revision": record.revision + 1,
                        "details": [
                            *record.to_wire()["details"],
                            {
                                "name": "reasoning",
                                "format": "text",
                                "value": {"text": "because", "omitted_bytes": 0},
                            },
                        ],
                    }
                )
            ]
        )
        view._refresh()
        await pilot.press("enter")
        await pilot.pause()

        panel = app.query_one(SpanDetailPanel)
        button = panel.query_one("#trajectory-span-detail-copy", Button)
        summary = panel.copy_text
        panel.set_tab(InspectorTab.REASONING)
        await pilot.pause()
        active_tab = panel.query_one("#trajectory-span-detail-tabs Tab.-active")
        reasoning = panel.copy_text
        assert reasoning != summary
        assert button.region.y == active_tab.region.y
        assert active_tab.region.right <= button.region.x

        await pilot.click(button)
        await pilot.pause()
        assert copied == [reasoning]


async def test_clicking_span_detail_text_copies_active_tab() -> None:
    copied: list[str] = []
    app = Host(copied=copied)
    async with app.run_test(size=(100, 30)) as pilot:
        await add_records(app)
        await pilot.press("enter")
        await pilot.pause()

        panel = app.query_one(SpanDetailPanel)
        log = panel.query_one("#trajectory-span-detail-content-summary", RichLog)
        assert log.tooltip is None
        content_x = log.content_region.x - log.region.x
        content_y = log.content_region.y - log.region.y
        await pilot.click(log, offset=(content_x, content_y))
        await pilot.pause()

        assert copied == [panel.copy_text]


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

        await pilot.press("enter")
        assert view.state.focus_region is FocusRegion.DETAIL
        assert view.query_one(SpanDetailPanel).record_id == "r3"
        await pilot.press("escape")
        assert view.state.focus_region is FocusRegion.TIMELINE
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


async def test_details_wait_for_the_cursor_to_rest_and_show_loading_meanwhile() -> None:
    app = Host()
    async with app.run_test(size=(120, 40)) as pilot:
        view = await add_records(app)
        view.focus_region(FocusRegion.TIMELINE)
        await pilot.pause(0.2)
        panel = view.query_one(SpanDetailPanel)
        loading = panel.query_one("#trajectory-span-detail-loading")
        assert panel.record_id == "r2" and not loading.display

        await pilot.press("h")
        await pilot.pause(0.1)
        assert panel.record_id == "r2" and loading.display  # still moving: not loaded yet
        await pilot.pause(0.5)
        assert panel.record_id == "r1" and not loading.display
