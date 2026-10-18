"""The trajectory view: record projection, search, keys, export and live refresh."""

from __future__ import annotations

import json
import math
from collections.abc import Callable

import pytest
from regie.trajectory.rich.enums import FocusRegion, TimelineLane
from regie.trajectory.rich.models import decode_delta, decode_page
from regie.trajectory.rich.projection import TrajectoryViewProjection
from regie.trajectory.rich.render.ordering import build_ordering
from regie.trajectory.rich.search import filter_matching_records
from regie.trajectory.rich.state import ParticipantTrajectoryState, TrajectoryStateStore
from regie.trajectory.rich.view import ReturnToTree, TrajectoryView
from regie.trajectory.rich.widgets.footer import TrajectoryFooter
from regie.trajectory.rich.widgets.span_detail import SpanDetailPanel
from regie.trajectory.rich.widgets.timeline import (
    Timeline,
    TimelineSpanClicked,
)
from regie.trajectory.ui_constants import (
    TIMELINE_LABEL_RIGHT_PADDING,
    TIMELINE_LABEL_WIDTH,
)
from textual.app import App, ComposeResult
from textual.widgets import Input

from packages.regie.tests.support.waiting import wait_until
from theater.frontend.trajectory import (
    GroupKind,
    PanelState,
    PanelStateInfo,
    TrajectoryGroup,
    TrajectoryPage,
    TrajectoryRecord,
    TrajectorySearchResult,
)


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
        participant_identity: Callable[[str], tuple[str | None, str | None]] | None = None,
    ) -> None:
        super().__init__()
        self.copied = copied if copied is not None else []
        self.state_store = state_store
        self.participant_identity = participant_identity
        self.returned = 0

    def compose(self) -> ComposeResult:
        yield TrajectoryView(
            "p1",
            copy_request=self.copied.append,
            state_store=self.state_store,
            participant_identity=self.participant_identity,
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


def test_filtered_export_uses_full_history_matches_the_timeline_shows() -> None:
    state = ParticipantTrajectoryState("p1")
    state.upsert([make_record("r9", "recent haystack")])
    state.query = "needle"
    state.filter_matches = True
    state.begin_search("needle")
    older = make_record("r1", "older needle")
    state.apply_search(TrajectorySearchResult(query="needle", records=(older,)))
    projection = TrajectoryViewProjection()

    visible = projection.refresh(state)

    assert [record.record_id for record in visible] == ["r1"]
    assert projection.matching_records(state) == (older,)


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


async def test_export_key_writes_both_files_and_copies_the_json_path(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copied: list[str] = []

    async def copy_path(path: str) -> bool:
        copied.append(path)
        return True

    monkeypatch.setattr("regie.trajectory.rich.export.trajectory_export_dir", lambda: tmp_path)
    monkeypatch.setattr("regie.trajectory.rich.view.copy_to_system_clipboard", copy_path)
    app = Host(participant_identity=lambda _participant_id: ("worker", "claude"))
    async with app.run_test(size=(100, 30)) as pilot:
        view = app.query_one(TrajectoryView)
        call, result = (
            TrajectoryRecord.from_wire(
                {
                    **make_record(record_id, summary, lane="tools").to_wire(),
                    "kind": kind,
                    "call_id": "c1",
                }
            )
            for record_id, summary, kind in (
                ("r1", "tool call", "tool_call"),
                ("r2", "needle result", "tool_result"),
            )
        )
        view.state.upsert([call, result, make_record("r3", "unrelated", turn_id=None)])
        view._refresh()
        view.focus_region(FocusRegion.TIMELINE)

        await pilot.press("slash", *"needle", "enter", "f", "E")
        await wait_until(pilot, lambda: len(tuple(tmp_path.glob("*.json"))) == 1)
        await wait_until(pilot, lambda: len(tuple(tmp_path.glob("*.md"))) == 1 and bool(copied))

        json_path = next(tmp_path.glob("*.json"))
        assert copied == [str(json_path)]
        document = json.loads(json_path.read_text())
        assert json_path.name.startswith("worker-")
        assert document["participant"] == {"id": "p1", "name": "worker", "harness": "claude"}
        assert document["filter"] == "needle"
        assert [record["record_id"] for record in document["records"]] == ["r1", "r2"]


@pytest.mark.parametrize("filtered", [False, True])
async def test_export_warns_when_no_records_are_visible(
    tmp_path, monkeypatch: pytest.MonkeyPatch, filtered: bool
) -> None:
    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr("regie.trajectory.rich.export.trajectory_export_dir", lambda: tmp_path)
    app = Host()
    monkeypatch.setattr(
        app,
        "notify",
        lambda message, **kwargs: notifications.append((str(message), kwargs["severity"])),
    )
    async with app.run_test(size=(100, 30)):
        view = app.query_one(TrajectoryView)
        if filtered:
            view.state.upsert([make_record("r1", "haystack")])
            view.state.query = "needle"
            view.state.filter_matches = True
            view._refresh()

        await view._export()

    assert notifications == [("nothing to export", "warning")]
    assert not tuple(tmp_path.iterdir())


async def test_export_failure_cleans_temporary_files_and_notifies_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notifications: list[tuple[str, str]] = []

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("disk failed")

    monkeypatch.setattr("regie.trajectory.rich.export.trajectory_export_dir", lambda: tmp_path)
    monkeypatch.setattr("regie.trajectory.rich.export.os.fsync", fail_fsync)
    app = Host()
    monkeypatch.setattr(
        app,
        "notify",
        lambda message, **kwargs: notifications.append((str(message), kwargs["severity"])),
    )
    async with app.run_test(size=(100, 30)) as pilot:
        view = await add_records(app)
        view.focus_region(FocusRegion.TIMELINE)

        await pilot.press("E")
        await wait_until(pilot, lambda: bool(notifications))

    assert notifications == [("trajectory export failed: disk failed", "error")]
    assert not tuple(tmp_path.iterdir())


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
    # An infinite settle window holds the move open, so loading is asserted at a
    # controlled point, not raced against a real clock; the test then releases it.
    monkeypatch.setattr("regie.trajectory.rich.view.TRAJECTORY_DETAIL_SETTLE_SECONDS", math.inf)
    app = Host()
    async with app.run_test(size=(120, 40)) as pilot:
        view = await add_records(app)
        view.focus_region(FocusRegion.TIMELINE)
        panel = view.query_one(SpanDetailPanel)
        loading = panel.query_one("#trajectory-span-detail-loading")
        await wait_until(pilot, lambda: panel.record_id == "r2" and not loading.display, timeout=10)

        view.action_move_span(-1)  # move synchronously: no await to pump the inf settle
        assert panel.record_id == "r2" and loading.display  # still moving: never settles yet

        monkeypatch.setattr("regie.trajectory.rich.view.TRAJECTORY_DETAIL_SETTLE_SECONDS", 0.01)
        view._schedule_detail_sync()  # release: let the now-rested cursor settle
        await wait_until(pilot, lambda: panel.record_id == "r1" and not loading.display, timeout=10)


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


def wire_view_record(
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


def view_record(
    record_id: str, *, index: int = 0, turn_id: str | None = "t1", **kwargs: object
) -> TrajectoryRecord:
    return TrajectoryRecord.from_wire(
        wire_view_record(record_id, index=index, turn_id=turn_id, **kwargs)
    )


async def populate_view(app: Host, records: list[TrajectoryRecord]) -> TrajectoryView:
    view = app.query_one(TrajectoryView)
    view.state.panel = PanelStateInfo(PanelState.READY, participant_state="live")
    view.state.upsert(records)
    view._refresh()
    return view


async def test_timeline_manual_scroll_continues_from_automatic_reveal() -> None:
    records = [
        view_record(
            f"r{index}",
            index=index,
            turn_id=None,
            timing={"start": index * 10, "end": index * 10 + 9, "provenance": "source"},
        )
        for index in range(40)
    ]
    app = Host()
    async with app.run_test(size=(50, 30)) as pilot:
        view = await populate_view(app, records)
        timeline = view.query_one(Timeline)
        await pilot.pause()

        timeline.set_scroll_offset(30)
        automatic_offset = timeline.horizontal_offset

        assert automatic_offset == 30
        assert timeline.scroll_target_x == automatic_offset

        timeline._scroll_left_for_pointer(animate=False)
        await pilot.pause()

        assert timeline.horizontal_offset == automatic_offset - app.scroll_sensitivity_x


async def test_tail_refresh_avoids_a_second_timeline_repaint(monkeypatch) -> None:
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = await populate_view(app, [view_record("r1")])
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


async def test_timeline_lane_labels_are_right_aligned() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = await populate_view(app, [view_record("model", index=0)])
        timeline = view.query_one(Timeline)
        model_middle = timeline.track_y(TimelineLane.MODEL)

        line = timeline.render_line(model_middle)

        assert line.text[:TIMELINE_LABEL_WIDTH] == "MODEL".rjust(
            TIMELINE_LABEL_WIDTH - TIMELINE_LABEL_RIGHT_PADDING
        ).ljust(TIMELINE_LABEL_WIDTH)


async def test_search_input_keeps_printable_navigation_keys() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        view = await populate_view(
            app, [view_record("r1"), view_record("r2", index=2, turn_id=None)]
        )
        search = app.query_one("#trajectory-search", Input)
        timeline = app.query_one(Timeline)
        await wait_until(pilot, lambda: app.focused is timeline)
        view.action_open_search()
        await wait_until(pilot, lambda: app.focused is search)
        await pilot.press("j")
        await wait_until(pilot, lambda: view.state.query == "j")
        assert app.focused is search  # navigation keys went to the input, not the timeline
        await pilot.press(*"klfdr y")
        await wait_until(pilot, lambda: view.state.query == "jklfdr y")
        assert app.query_one("#trajectory-search", Input).has_focus


def test_ordering_emits_many_nested_group_records_once_in_source_order() -> None:
    records = [view_record(f"r{index}", index=index) for index in range(128)]
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
        view = await populate_view(
            app, [view_record("r1"), view_record("r2", index=2, turn_id=None)]
        )
        view.query_one(Timeline).set_hovered("r1")
        assert view.state.follow_tail
        view.on_timeline_span_clicked(TimelineSpanClicked("r1"))
        assert not view.state.follow_tail
        view.action_move_span(1)
        assert view.state.follow_tail  # reaching the last span follows the live tail again
        view.action_move_span(-1)
        assert not view.state.follow_tail


def test_oversized_canonical_page_and_delta_are_rejected() -> None:
    records = [wire_view_record(str(index), summary="x" * 16_000) for index in range(70)]
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


async def test_reopening_search_keeps_text_typed_before_it_was_reported() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        view = await populate_view(app, [view_record("r1")])
        await pilot.pause()
        view.action_open_search()
        search = app.query_one("#trajectory-search", Input)
        search.insert_text_at_cursor("j")  # its change event is still queued
        view._finish_mount()  # a late mount step reopens the search in between
        await wait_until(pilot, lambda: view.state.query == "j")

        assert search.value == "j"
