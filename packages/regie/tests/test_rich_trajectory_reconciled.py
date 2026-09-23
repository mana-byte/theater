from __future__ import annotations

import pytest
from regie.trajectory.domain import (
    GroupKind,
    PanelState,
    PanelStateInfo,
    TrajectoryGroup,
    TrajectoryRecord,
)
from regie.trajectory.rich.enums import InspectorTab, TimelineLane
from regie.trajectory.rich.inspection.links import DETAIL_PARTICIPANT_META
from regie.trajectory.rich.inspection.project import detail_text, tabs_for_record
from regie.trajectory.rich.inspection.styled import build_span_details
from regie.trajectory.rich.models import decode_delta, decode_page
from regie.trajectory.rich.render.ordering import build_ordering
from regie.trajectory.rich.view import TrajectoryParticipantSelected, TrajectoryView
from regie.trajectory.rich.widgets.span_detail import SpanDetailPanel
from regie.trajectory.rich.widgets.timeline import (
    Timeline,
    TimelineSpanClicked,
)
from regie.trajectory.ui_constants import (
    TIMELINE_LABEL_RIGHT_PADDING,
    TIMELINE_LABEL_WIDTH,
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
        model_middle = 1 + list(TimelineLane).index(TimelineLane.MODEL) * timeline.lane_height

        line = timeline.render_line(model_middle)

        assert line.text[:TIMELINE_LABEL_WIDTH] == "MODEL".rjust(
            TIMELINE_LABEL_WIDTH - TIMELINE_LABEL_RIGHT_PADDING
        ).ljust(TIMELINE_LABEL_WIDTH)


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
