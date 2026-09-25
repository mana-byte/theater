from __future__ import annotations

import pytest
from regie.trajectory.rich.enums import TimelineLane
from regie.trajectory.rich.models import decode_delta, decode_page
from regie.trajectory.rich.render.ordering import build_ordering
from regie.trajectory.rich.view import TrajectoryView
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

from tests.rig.waiting import wait_until
from theater.frontend.trajectory import (
    GroupKind,
    PanelState,
    PanelStateInfo,
    TrajectoryGroup,
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


async def test_timeline_manual_scroll_continues_from_automatic_reveal() -> None:
    records = [
        record(
            f"r{index}",
            index=index,
            turn_id=None,
            timing={"start": index * 10, "end": index * 10 + 9, "provenance": "source"},
        )
        for index in range(40)
    ]
    app = Host()
    async with app.run_test(size=(50, 30)) as pilot:
        view = await populate(app, records)
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


async def test_timeline_lane_labels_are_right_aligned() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = await populate(app, [record("model", index=0)])
        timeline = view.query_one(Timeline)
        model_middle = timeline.track_y(TimelineLane.MODEL)

        line = timeline.render_line(model_middle)

        assert line.text[:TIMELINE_LABEL_WIDTH] == "MODEL".rjust(
            TIMELINE_LABEL_WIDTH - TIMELINE_LABEL_RIGHT_PADDING
        ).ljust(TIMELINE_LABEL_WIDTH)


async def test_search_input_keeps_printable_navigation_keys() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        view = await populate(app, [record("r1"), record("r2", index=2, turn_id=None)])
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


async def test_reopening_search_keeps_text_typed_before_it_was_reported() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        view = await populate(app, [record("r1")])
        await pilot.pause()
        view.action_open_search()
        search = app.query_one("#trajectory-search", Input)
        search.insert_text_at_cursor("j")  # its change event is still queued
        view._finish_mount()  # a late mount step reopens the search in between
        await wait_until(pilot, lambda: view.state.query == "j")

        assert search.value == "j"
