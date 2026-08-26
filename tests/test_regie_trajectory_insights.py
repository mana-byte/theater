from __future__ import annotations

import json

from textual.app import App, ComposeResult
from textual.coordinate import Coordinate
from textual.widgets import Static

from theater.regie.trajectory.badges import provenance_badges
from theater.regie.trajectory.breadcrumb import TrajectoryBreadcrumb, breadcrumb_text
from theater.regie.trajectory.constants import (
    TRAJECTORY_INSIGHT_AUXILIARY_ROW_HEIGHT,
    TRAJECTORY_INSIGHT_HOVERED_SPAN_ROW_HEIGHT,
    TRAJECTORY_INSIGHT_SPAN_ROW_HEIGHT,
)
from theater.regie.trajectory.enums import DiagnosticView, FocusRegion
from theater.regie.trajectory.insights import InsightsPanel
from theater.regie.trajectory.ledger import Ledger
from theater.regie.trajectory.span_detail import SpanDetailPanel
from theater.regie.trajectory.view import TrajectoryView
from theater.trajectory import (
    ContentFormat,
    CostProvenance,
    DetailField,
    LinkDirection,
    ParticipantLink,
    Timing,
    TimingProvenance,
    TrajectoryDelta,
    TrajectoryFailure,
    TrajectoryFailureCategory,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryStatus,
    TrajectoryUpsert,
    TrajectoryUsage,
)


def _record(
    record_id: str,
    index: int,
    *,
    lane: TrajectoryLane = TrajectoryLane.MODEL,
    kind: TrajectoryKind = TrajectoryKind.ASSISTANT,
    status: TrajectoryStatus = TrajectoryStatus.COMPLETED,
    request_id: str | None = None,
    call_id: str | None = None,
    timing: Timing | None = None,
    usage: TrajectoryUsage | None = None,
    failure: TrajectoryFailure | None = None,
    links: tuple[ParticipantLink, ...] = (),
    details: tuple[DetailField, ...] = (),
    turn_id: str = "turn-1",
) -> TrajectoryRecord:
    return TrajectoryRecord(
        record_id=record_id,
        revision=1,
        participant_id="p1",
        source_epoch="epoch",
        lane=lane,
        kind=kind,
        source="codex",
        summary=record_id,
        status=status,
        raw_index=index,
        turn_id=turn_id,
        request_id=request_id,
        call_id=call_id,
        timing=timing,
        usage=usage,
        failure=failure,
        links=links,
        details=details,
    )


class _Host(App):
    def compose(self) -> ComposeResult:
        yield TrajectoryView("p1", id="trajectory")


class _LinkHost(_Host):
    def __init__(self) -> None:
        super().__init__()
        self.selected = []

    def on_trajectory_participant_selected(self, message) -> None:
        self.selected.append(message)


async def test_specialized_views_reuse_one_table_and_restore_the_ledger_after_details() -> None:
    records = (
        _record(
            "request",
            1,
            request_id="req",
            timing=Timing(1, 4, 3_000, TimingProvenance.SOURCE, first_token=2),
            usage=TrajectoryUsage(
                model="gpt",
                input_tokens=100,
                output_tokens=20,
                cost_usd=0.01,
                cost_provenance=CostProvenance.ESTIMATED,
            ),
        ),
        _record(
            "call",
            2,
            lane=TrajectoryLane.TOOLS,
            kind=TrajectoryKind.TOOL_CALL,
            request_id="req",
            call_id="tool",
            timing=Timing(start=2, provenance=TimingProvenance.SOURCE),
        ),
        _record(
            "result",
            3,
            lane=TrajectoryLane.TOOLS,
            kind=TrajectoryKind.TOOL_RESULT,
            request_id="req",
            call_id="tool",
            timing=Timing(end=3, provenance=TimingProvenance.SOURCE),
        ),
        _record(
            "error",
            4,
            kind=TrajectoryKind.ERROR,
            status=TrajectoryStatus.ERROR,
            failure=TrajectoryFailure(TrajectoryFailureCategory.PROVIDER, code="rate_limit"),
        ),
    )
    app = _Host()
    async with app.run_test(size=(120, 38)) as pilot:
        view = app.query_one(TrajectoryView)
        view.state.upsert(records)
        view.action_set_diagnostic_view(DiagnosticView.WATERFALL)
        await pilot.pause()

        insights = view.query_one(InsightsPanel)
        assert insights.insight_count == 2
        assert [row.height for row in insights.ordered_rows] == [
            TRAJECTORY_INSIGHT_SPAN_ROW_HEIGHT,
            TRAJECTORY_INSIGHT_HOVERED_SPAN_ROW_HEIGHT,
        ]
        assert all(not str(cell).startswith("\n") for cell in insights.get_row_at(0))
        assert all(
            str(cell).startswith("\n") and not str(cell).startswith("\n\n")
            for cell in insights.get_row_at(1)
        )
        assert insights.cursor_row == 1
        assert not insights.has_class("-hidden")
        assert view.query_one(Ledger).has_class("-hidden")
        assert view.active_region is FocusRegion.INSIGHTS
        assert app.focused is insights

        path = view.query_one("#trajectory-breadcrumb-path", Static)
        badges = view.query_one("#trajectory-breadcrumb-badges", Static)
        assert "REQUEST" in str(path.render())
        assert "TIMING" in str(badges.render())
        assert "COST" in str(badges.render())

        await pilot.press("enter")
        assert view.active_region is FocusRegion.DETAIL
        assert not view.query_one(SpanDetailPanel).has_class("-hidden")
        await pilot.press("escape")
        assert view.active_region is FocusRegion.INSIGHTS
        assert app.focused is insights

        view.action_set_diagnostic_view(DiagnosticView.ALL)
        assert not view.query_one(Ledger).has_class("-hidden")
        assert insights.has_class("-hidden")


async def test_file_view_lists_chronological_operations_and_opens_exact_spans() -> None:
    records = (
        _record(
            "read-call",
            1,
            lane=TrajectoryLane.TOOLS,
            kind=TrajectoryKind.TOOL_CALL,
            call_id="read",
            details=(
                DetailField.from_text("tool", "read_file"),
                DetailField.from_text(
                    "arguments",
                    json.dumps({"file_path": "src/shared.py"}),
                    format=ContentFormat.JSON,
                ),
            ),
        ),
        _record(
            "read-result",
            2,
            lane=TrajectoryLane.TOOLS,
            kind=TrajectoryKind.TOOL_RESULT,
            call_id="read",
        ),
        _record(
            "write-call",
            3,
            lane=TrajectoryLane.TOOLS,
            kind=TrajectoryKind.TOOL_CALL,
            call_id="write",
            status=TrajectoryStatus.ERROR,
            details=(
                DetailField.from_text("tool", "write_file"),
                DetailField.from_text(
                    "arguments",
                    json.dumps({"file_path": "src/shared.py"}),
                    format=ContentFormat.JSON,
                ),
            ),
        ),
    )
    app = _Host()
    async with app.run_test(size=(120, 38)) as pilot:
        view = app.query_one(TrajectoryView)
        view.state.upsert(records)
        view.action_set_diagnostic_view(DiagnosticView.FILES)
        await pilot.pause()

        insights = view.query_one(InsightsPanel)
        panel = view.query_one(SpanDetailPanel)
        assert insights.insight_count == 3
        assert [row.height for row in insights.ordered_rows] == [
            TRAJECTORY_INSIGHT_AUXILIARY_ROW_HEIGHT,
            TRAJECTORY_INSIGHT_SPAN_ROW_HEIGHT,
            TRAJECTORY_INSIGHT_HOVERED_SPAN_ROW_HEIGHT,
        ]
        assert "src/shared.py" in str(insights.get_row_at(0)[1])
        assert "├─ read_file" in str(insights.get_row_at(1)[1])
        assert "└─ write_file" in str(insights.get_row_at(2)[1])
        assert insights.cursor_row == 2

        await pilot.press("k", "enter")
        assert view.state.detail_id == "read-call"
        assert panel.record_id == "read-call"
        await pilot.press("escape")
        assert app.focused is insights

        await pilot.press("k", "enter")
        assert view.state.detail_id is None
        assert panel.has_class("-hidden")

        region = insights._get_cell_region(Coordinate(2, 1))
        await pilot.click(insights, offset=(region.x + 1, region.y))
        await pilot.pause()
        assert view.state.detail_id == "write-call"
        assert panel.record_id == "write-call"

        await pilot.press("escape")


async def test_pointer_hover_expands_only_insight_spans() -> None:
    records = (
        _record(
            "read-call",
            1,
            lane=TrajectoryLane.TOOLS,
            kind=TrajectoryKind.TOOL_CALL,
            call_id="read",
            details=(
                DetailField.from_text("tool", "read_file"),
                DetailField.from_text(
                    "arguments",
                    json.dumps({"file_path": "src/shared.py"}),
                    format=ContentFormat.JSON,
                ),
            ),
        ),
        _record(
            "write-call",
            2,
            lane=TrajectoryLane.TOOLS,
            kind=TrajectoryKind.TOOL_CALL,
            call_id="write",
            details=(
                DetailField.from_text("tool", "write_file"),
                DetailField.from_text(
                    "arguments",
                    json.dumps({"file_path": "src/shared.py"}),
                    format=ContentFormat.JSON,
                ),
            ),
        ),
    )
    app = _Host()
    async with app.run_test(size=(120, 38)) as pilot:
        view = app.query_one(TrajectoryView)
        view.state.upsert(records)
        view.action_set_diagnostic_view(DiagnosticView.FILES)
        await pilot.pause()
        insights = view.query_one(InsightsPanel)

        directory_region = insights._get_cell_region(Coordinate(0, 0))
        await pilot.hover(insights, offset=(directory_region.x, directory_region.y))
        assert insights.ordered_rows[0].height == TRAJECTORY_INSIGHT_AUXILIARY_ROW_HEIGHT

        first_region = insights._get_cell_region(Coordinate(1, 0))
        await pilot.hover(insights, offset=(first_region.x, first_region.y))
        assert insights.ordered_rows[1].height == TRAJECTORY_INSIGHT_HOVERED_SPAN_ROW_HEIGHT
        assert insights.get_row_at(1)[1].plain.startswith("\n")
        assert not insights.get_row_at(1)[1].plain.startswith("\n\n")

        second_region = insights._get_cell_region(Coordinate(2, 0))
        await pilot.hover(insights, offset=(second_region.x, second_region.y))
        assert insights.ordered_rows[1].height == TRAJECTORY_INSIGHT_SPAN_ROW_HEIGHT
        assert insights.ordered_rows[2].height == TRAJECTORY_INSIGHT_HOVERED_SPAN_ROW_HEIGHT

        await pilot.hover("#trajectory-search")
        assert [row.height for row in insights.ordered_rows] == [
            TRAJECTORY_INSIGHT_AUXILIARY_ROW_HEIGHT,
            TRAJECTORY_INSIGHT_SPAN_ROW_HEIGHT,
            TRAJECTORY_INSIGHT_HOVERED_SPAN_ROW_HEIGHT,
        ]

        await pilot.press("k")
        assert [row.height for row in insights.ordered_rows] == [
            TRAJECTORY_INSIGHT_AUXILIARY_ROW_HEIGHT,
            TRAJECTORY_INSIGHT_HOVERED_SPAN_ROW_HEIGHT,
            TRAJECTORY_INSIGHT_SPAN_ROW_HEIGHT,
        ]


async def test_waterfall_follow_moves_to_each_newest_tool_operation() -> None:
    initial = (
        _record("request", 1, request_id="req"),
        _record(
            "call-1",
            2,
            lane=TrajectoryLane.TOOLS,
            kind=TrajectoryKind.TOOL_CALL,
            request_id="req",
            call_id="tool-1",
        ),
        _record(
            "result-1",
            3,
            lane=TrajectoryLane.TOOLS,
            kind=TrajectoryKind.TOOL_RESULT,
            request_id="req",
            call_id="tool-1",
        ),
    )
    app = _Host()
    async with app.run_test(size=(100, 30)):
        view = app.query_one(TrajectoryView)
        view.state.upsert(initial)
        view.action_set_diagnostic_view(DiagnosticView.WATERFALL)
        insights = view.query_one(InsightsPanel)

        assert insights.cursor_row == 1

        additions = (
            _record(
                "call-2",
                4,
                lane=TrajectoryLane.TOOLS,
                kind=TrajectoryKind.TOOL_CALL,
                request_id="req",
                call_id="tool-2",
            ),
            _record(
                "result-2",
                5,
                lane=TrajectoryLane.TOOLS,
                kind=TrajectoryKind.TOOL_RESULT,
                request_id="req",
                call_id="tool-2",
            ),
            _record("answer-2", 6, request_id="req"),
        )
        view.state.apply_follow(
            TrajectoryDelta("stream", upserts=tuple(TrajectoryUpsert(item) for item in additions))
        )
        view._refresh()

        assert view.state.follow_tail
        assert view.state.selected_id == "call-2"
        assert insights.cursor_row == 2


async def test_waterfall_tail_ignores_later_records_folded_into_request_row() -> None:
    records = (
        _record("request", 1, request_id="req"),
        _record(
            "call",
            2,
            lane=TrajectoryLane.TOOLS,
            kind=TrajectoryKind.TOOL_CALL,
            request_id="req",
            call_id="tool",
        ),
        _record(
            "result",
            3,
            lane=TrajectoryLane.TOOLS,
            kind=TrajectoryKind.TOOL_RESULT,
            request_id="req",
            call_id="tool",
        ),
        _record("answer", 4, request_id="req"),
    )
    app = _Host()
    async with app.run_test(size=(100, 30)) as pilot:
        view = app.query_one(TrajectoryView)
        view.state.upsert(records)
        view.action_set_diagnostic_view(DiagnosticView.WATERFALL)
        insights = view.query_one(InsightsPanel)

        assert view.state.selected_id == "call"
        assert insights.cursor_row == 1

        view.state.pause_follow()
        view.state.select("request")
        view._refresh()
        await pilot.press("G")

        assert view.state.follow_tail
        assert view.state.selected_id == "call"
        assert insights.cursor_row == 1


async def test_resource_spans_are_one_line_and_turn_rows_remain_two_lines() -> None:
    records = tuple(
        _record(
            f"request-{index}",
            index,
            request_id=f"req-{index}",
            turn_id=f"turn-{index}",
            usage=TrajectoryUsage(
                model="gpt",
                input_tokens=index * 100,
                output_tokens=index * 10,
            ),
        )
        for index in range(1, 7)
    )
    app = _Host()
    async with app.run_test(size=(100, 30)) as pilot:
        view = app.query_one(TrajectoryView)
        view.state.upsert(records)
        view.action_set_diagnostic_view(DiagnosticView.RESOURCES)
        await pilot.pause()

        insights = view.query_one(InsightsPanel)
        row_count = len(insights.ordered_rows)
        assert row_count == 12
        assert [row.height for row in insights.ordered_rows] == [
            TRAJECTORY_INSIGHT_AUXILIARY_ROW_HEIGHT,
            TRAJECTORY_INSIGHT_SPAN_ROW_HEIGHT,
        ] * 5 + [
            TRAJECTORY_INSIGHT_AUXILIARY_ROW_HEIGHT,
            TRAJECTORY_INSIGHT_HOVERED_SPAN_ROW_HEIGHT,
        ]
        assert all(
            str(cell).startswith("\n") and not str(cell).startswith("\n\n")
            for cell in insights.get_row_at(0)
        )
        assert all(not str(cell).startswith("\n") for cell in insights.get_row_at(1))
        assert insights.cursor_row == row_count - 1

        await pilot.press(*(["k"] * (row_count - 1)))
        assert insights.cursor_row == 0
        assert insights.scroll_y == 0

        await pilot.press(*(["j"] * (row_count - 1)))
        assert insights.cursor_row == row_count - 1
        assert insights.scroll_y > 0


async def test_failure_view_uses_navigator_instead_of_generic_ledger() -> None:
    app = _Host()
    async with app.run_test(size=(100, 30)):
        view = app.query_one(TrajectoryView)
        view.state.upsert(
            (
                _record(
                    "error",
                    1,
                    kind=TrajectoryKind.ERROR,
                    status=TrajectoryStatus.ERROR,
                    failure=TrajectoryFailure(
                        TrajectoryFailureCategory.TRANSPORT,
                        code="connection_lost",
                    ),
                ),
            )
        )
        view.action_set_diagnostic_view(DiagnosticView.ERRORS)

        insights = view.query_one(InsightsPanel)
        assert insights.insight_count == 1
        assert insights.ordered_rows[0].height == TRAJECTORY_INSIGHT_HOVERED_SPAN_ROW_HEIGHT
        assert all(str(cell).startswith("\n") for cell in insights.get_row_at(0))
        assert view.active_region is FocusRegion.INSIGHTS
        assert any("FAILURE" in str(column.label) for column in insights.columns.values())


async def test_delegation_rows_open_the_exact_linked_participant_event() -> None:
    app = _LinkHost()
    async with app.run_test(size=(100, 30)) as pilot:
        view = app.query_one(TrajectoryView)
        view.state.upsert(
            (
                _record(
                    "send",
                    1,
                    lane=TrajectoryLane.THEATER,
                    kind=TrajectoryKind.SEND,
                    links=(
                        ParticipantLink(
                            "p2",
                            "child",
                            LinkDirection.OUTGOING,
                            target_record_id="target",
                        ),
                    ),
                ),
            )
        )
        view.action_set_diagnostic_view(DiagnosticView.DELEGATION)
        assert view.query_one(InsightsPanel).ordered_rows[0].height == 2
        await pilot.press("enter")
        await pilot.pause()

        assert len(app.selected) == 1
        assert app.selected[0].participant_id == "p2"
        assert app.selected[0].target_record_id == "target"


def test_breadcrumb_marks_unknown_provenance_instead_of_implying_zero() -> None:
    record = _record("plain", 1)

    assert "PLAIN" in breadcrumb_text(record).plain.upper()
    badges = provenance_badges(record).plain
    assert "TIMING" in badges and "unavailable" in badges
    assert "COST" in badges and "unknown" in badges
    assert TrajectoryBreadcrumb.can_focus is False
