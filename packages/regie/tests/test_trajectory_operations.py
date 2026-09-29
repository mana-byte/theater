from __future__ import annotations

from dataclasses import replace

import pytest
from regie.trajectory.rich.inspection.content import Palette
from regie.trajectory.rich.inspection.sheet import build_sheet
from regie.trajectory.rich.projection import TrajectoryViewProjection
from regie.trajectory.rich.render.tools import build_tool_index
from regie.trajectory.rich.state import ParticipantTrajectoryState
from regie.trajectory.rich.view import TrajectoryView
from regie.trajectory.rich.widgets.span_detail import SpanDetailPanel
from regie.trajectory.rich.widgets.timeline import Timeline
from textual.app import App, ComposeResult

from tests.rig.waiting import wait_until
from theater.frontend.trajectory import (
    DetailField,
    ParticipantLink,
    Timing,
    TimingProvenance,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectorySearchResult,
    TrajectoryStatus,
    tool_operations_for_records,
)


def _pair(server: str = "theater") -> tuple[TrajectoryRecord, TrajectoryRecord]:
    theater = server == "theater"
    name = {"theater": "spawn_session", "theater_wait": "await_sessions", "github": "search"}[
        server
    ]
    call = TrajectoryRecord(
        record_id="call",
        revision=1,
        participant_id="p1",
        source_epoch="epoch",
        lane=TrajectoryLane.THEATER if theater else TrajectoryLane.TOOLS,
        kind=TrajectoryKind.THEATER_CALL if theater else TrajectoryKind.TOOL_CALL,
        source="harness",
        summary=name,
        status=TrajectoryStatus.PENDING,
        raw_index=1,
        call_id="c1",
        mcp_server=server,
        mcp_tool=name,
        timing=Timing(start=100, provenance=TimingProvenance.SOURCE),
        details=(DetailField.from_text("arguments", '{"name": "worker"}'),),
    )
    result = replace(
        call,
        record_id="result",
        kind=TrajectoryKind.THEATER_RESULT if theater else TrajectoryKind.TOOL_RESULT,
        status=TrajectoryStatus.ERROR,
        raw_index=2,
        summary=f"{name} failed",
        timing=Timing(end=104, provenance=TimingProvenance.SOURCE),
        details=(DetailField.from_text("result", "uniqueresultneedle"),),
        links=(ParticipantLink("child", "target"),),
    )
    return call, result


class _Host(App):
    def compose(self) -> ComposeResult:
        yield TrajectoryView("p1")


@pytest.mark.parametrize("server", ["theater", "github", "theater_wait"])
async def test_live_operation_updates_one_named_span_and_preserves_its_records(server: str) -> None:
    call, result = _pair(server)
    async with _Host().run_test(size=(100, 35)) as pilot:
        view = pilot.app.query_one(TrajectoryView)
        timeline = view.query_one(Timeline)
        panel = view.query_one(SpanDetailPanel)
        view.state.upsert((call,))
        view._refresh()
        assert timeline.projection.spans[0].point

        view.state.upsert((result,))
        view.state.select(result.record_id)
        view._refresh()
        await wait_until(
            pilot, lambda: any(section.title == "Result" for section in panel.sections)
        )

        assert timeline.span_ids == (call.record_id,)
        assert view.state.selected_id == call.record_id
        span = timeline.projection.spans[0]
        assert span.lane.value == ("theater" if server == "theater" else "mcp")
        assert not span.point
        assert timeline.records[0].status is TrajectoryStatus.ERROR
        style = timeline._span_style(span)
        assert style.bgcolor == timeline._component("error").color  # selected: solid fill
        assert not style.italic
        assert panel._sheet is not None
        assert f"{server} › {call.mcp_tool}" in panel._sheet.title.plain
        assert "4.0s" in panel._sheet.title.plain
        assert [section.title for section in panel.sections] == [
            "Input",
            "Result",
            "Participants",
            "Debug",
        ]
        assert "worker" in panel.page_copy_text and "uniqueresultneedle" in panel.page_copy_text
        assert view.state.record_list == [call, result]
        if server == "theater":
            assert tool_operations_for_records(view.state.record_list) == ()


def test_theater_pairing_keeps_domains_epochs_and_unkeyed_records_separate() -> None:
    call, result = _pair()
    ordinary = replace(
        result, record_id="ordinary", lane=TrajectoryLane.TOOLS, kind=TrajectoryKind.TOOL_RESULT
    )
    other_epoch = replace(result, record_id="other-epoch", source_epoch="other")
    unkeyed = (
        replace(call, record_id="unkeyed-call", call_id=None),
        replace(result, record_id="unkeyed-result", call_id=None),
    )
    index = build_tool_index((call, result, ordinary, other_epoch, *unkeyed))

    assert len(index.ordered) == 5
    assert index.by_record_id[call.record_id] == index.by_record_id[result.record_id]
    assert len(set(index.by_record_id.values())) == 5


@pytest.mark.parametrize("case", ["loaded_partner", "remote_pair", "orphan_result"])
def test_full_history_search_keeps_operation_matches_and_available_context(case: str) -> None:
    call, result = _pair()
    state = ParticipantTrajectoryState("p1")
    if case == "loaded_partner":
        state.upsert((call, replace(result, status=TrajectoryStatus.PARTIAL)))
        result = replace(result, revision=2)
    query = "spawn_session" if case == "remote_pair" else "uniqueresultneedle"
    state.query = query
    state.filter_matches = True
    state.begin_search(query)
    returned = (call, result) if case == "remote_pair" else (result,)
    state.apply_search(TrajectorySearchResult(query=query, records=returned))
    projection = TrajectoryViewProjection()

    visible = projection.refresh(state)

    assert len(visible) == 1
    assert visible[0].status is TrajectoryStatus.ERROR
    assert projection.matched_ids == {visible[0].record_id}
    expected = (result,) if case == "orphan_result" else (call, result)
    assert projection.matching_records(state) == expected
    operation_id = state.tool_index.by_record_id[visible[0].record_id]
    sheet = build_sheet(visible[0], Palette(), tool=state.tool_index.by_id[operation_id])
    assert "theater › spawn_session" in sheet.title.plain
    assert "uniqueresultneedle" in sheet.copy_text
    assert ("worker" in sheet.copy_text) is (case != "orphan_result")

    state.query = ""
    state.begin_search("")
    assert len(state.tool_index.ordered) == (1 if case == "loaded_partner" else 0)
