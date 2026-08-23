from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Input

from theater.regie.trajectory.enums import FocusRegion
from theater.regie.trajectory.inspector import Inspector
from theater.regie.trajectory.ledger import Ledger
from theater.regie.trajectory.state import TrajectoryStateStore
from theater.regie.trajectory.timeline import Timeline
from theater.regie.trajectory.view import ReturnToTree, TrajectoryView
from theater.trajectory import PanelState, PanelStateInfo, TrajectoryRecord


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


async def test_surface_uses_fixed_timeline_and_virtualized_ledger() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = await add_records(app)

        assert isinstance(view.query_one("#trajectory-timeline"), Timeline)
        assert isinstance(view.query_one("#trajectory-ledger"), Ledger)
        assert isinstance(view.query_one("#trajectory-inspector"), Inspector)
        assert len(view.query(Ledger)) == 1
        assert len(view.query(Inspector)) == 1


async def test_keys_route_regions_selection_search_reset_and_escape() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        view = await add_records(app)
        assert view.state.selected_id == "r2"

        await pilot.press("k")
        assert view.state.selected_id == "r1"
        await pilot.press("enter")
        assert view.state.focus_region is FocusRegion.INSPECTOR
        old_tab = view.state.inspector_tab
        await pilot.press("l")
        assert view.state.inspector_tab != old_tab
        await pilot.press("/")
        await pilot.press(*"second")
        assert view.state.query == "second"
        view.action_reset()
        assert view.state.query == ""
        view.focus_region(FocusRegion.LEDGER)
        await pilot.press("escape")
        assert app.returned == 1


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


async def test_inspector_maximizes_by_key_and_header_double_click() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        view = await add_records(app)
        await pilot.press("enter", "m")
        assert view.state.inspector_maximized

        await pilot.click("#trajectory-inspector", offset=(1, 0), times=2)
        assert not view.state.inspector_maximized


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
