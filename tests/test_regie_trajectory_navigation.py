from __future__ import annotations

from textual.app import App, ComposeResult

from theater.regie.trajectory.details import (
    DETAIL_PARTICIPANT_CORRELATION_KEY_META,
    DETAIL_PARTICIPANT_CORRELATION_TYPE_META,
    DETAIL_PARTICIPANT_DIRECTION_META,
    DETAIL_PARTICIPANT_EXACT_META,
    DETAIL_PARTICIPANT_META,
    DETAIL_PARTICIPANT_RELATION_META,
    DETAIL_PARTICIPANT_TARGET_META,
    DETAIL_PARTICIPANT_UNRESOLVED_META,
    build_span_details,
    participant_link_from_meta,
)
from theater.regie.trajectory.enums import DiagnosticView, InspectorTab
from theater.regie.trajectory.navigation import (
    TrajectoryNavigationHistory,
    TrajectoryNavigationTarget,
)
from theater.regie.trajectory.span_detail import SpanDetailParticipantLinkClicked
from theater.regie.trajectory.view import (
    TrajectoryBackRequested,
    TrajectoryParticipantSelected,
    TrajectoryView,
)
from theater.trajectory import (
    PanelState,
    PanelStateInfo,
    ParticipantLink,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryStatus,
)


def _record(
    record_id: str,
    index: int,
    *,
    lane: TrajectoryLane = TrajectoryLane.MODEL,
    kind: TrajectoryKind = TrajectoryKind.ASSISTANT,
    status: TrajectoryStatus = TrajectoryStatus.COMPLETED,
    call_id: str | None = None,
    links: tuple[ParticipantLink, ...] = (),
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
        call_id=call_id,
        links=links,
    )


class _Host(App):
    def __init__(self) -> None:
        super().__init__()
        self.selected: list[TrajectoryParticipantSelected] = []
        self.back: list[TrajectoryBackRequested] = []

    def compose(self) -> ComposeResult:
        yield TrajectoryView("p1", id="trajectory")

    def on_trajectory_participant_selected(self, message: TrajectoryParticipantSelected) -> None:
        self.selected.append(message)

    def on_trajectory_back_requested(self, message: TrajectoryBackRequested) -> None:
        self.back.append(message)


def test_navigation_history_is_bounded_and_skips_adjacent_duplicates() -> None:
    history = TrajectoryNavigationHistory(limit=2)

    assert history.push("p1", "r1")
    assert not history.push("p1", "r1")
    assert history.push("p2", "r2")
    assert history.push("p3", "r3")
    assert history.entries == (
        TrajectoryNavigationTarget("p2", "r2"),
        TrajectoryNavigationTarget("p3", "r3"),
    )
    assert history.back() == TrajectoryNavigationTarget("p3", "r3")
    assert history.back() == TrajectoryNavigationTarget("p2", "r2")
    assert history.back() is None


def test_detail_link_metadata_preserves_only_bounded_link_primitives() -> None:
    link = ParticipantLink(
        "p2",
        "child",
        target_record_id="target",
        correlation_type="job_handle",
        correlation_key="job-1",
    )
    details = build_span_details(_record("source", 1, links=(link,)), InspectorTab.SUMMARY)
    metadata = next(
        meta
        for span in details.content.spans
        if (meta := getattr(span.style, "meta", {})) and DETAIL_PARTICIPANT_META in meta
    )

    assert metadata == {
        DETAIL_PARTICIPANT_META: "p2",
        DETAIL_PARTICIPANT_RELATION_META: "child",
        DETAIL_PARTICIPANT_DIRECTION_META: "related",
        DETAIL_PARTICIPANT_TARGET_META: "target",
        DETAIL_PARTICIPANT_CORRELATION_TYPE_META: "job_handle",
        DETAIL_PARTICIPANT_CORRELATION_KEY_META: "job-1",
        DETAIL_PARTICIPANT_EXACT_META: "1",
        DETAIL_PARTICIPANT_UNRESOLVED_META: "0",
    }
    assert all(isinstance(value, str) for value in metadata.values())
    assert "exact target target" in details.copy_text
    assert participant_link_from_meta(metadata) == link
    clicked = SpanDetailParticipantLinkClicked(link, exact=True, unresolved=False)
    assert clicked.link == link
    assert clicked.exact and not clicked.unresolved


async def test_exact_links_request_target_selection_and_back_is_keyboard_accessible() -> None:
    link = ParticipantLink("p2", "child", target_record_id="target")
    source = _record("source", 1, links=(link,))
    app = _Host()
    async with app.run_test(size=(80, 40)) as pilot:
        view = app.query_one(TrajectoryView)
        view.state.panel = PanelStateInfo(PanelState.READY, participant_state="live")
        view.state.upsert((source,))
        view.state.select("source")
        view._refresh()

        view.on_span_detail_participant_link_clicked(
            SpanDetailParticipantLinkClicked(link, exact=True, unresolved=False)
        )
        await pilot.pause()

        assert app.selected[-1].participant_id == "p2"
        assert app.selected[-1].target_record_id == "target"
        assert app.selected[-1].exact
        assert app.selected[-1].link == link

        await pilot.press("b")

        assert len(app.back) == 1


async def test_select_and_reveal_exact_loaded_record_clears_only_needed_filters() -> None:
    call = _record(
        "call",
        1,
        lane=TrajectoryLane.TOOLS,
        kind=TrajectoryKind.TOOL_CALL,
        call_id="tool",
    )
    result = _record(
        "result",
        2,
        lane=TrajectoryLane.TOOLS,
        kind=TrajectoryKind.TOOL_RESULT,
        call_id="tool",
    )
    hidden = _record("hidden", 3)
    later = _record("later", 4)
    app = _Host()
    async with app.run_test(size=(100, 30)):
        view = app.query_one(TrajectoryView)
        view.state.panel = PanelStateInfo(PanelState.READY, participant_state="live")
        view.state_store.page_size = 1
        view.state.upsert((call, result, hidden, later))
        view.state.diagnostic_view = DiagnosticView.ERRORS
        view.state.query = "call"
        view.state.lane_filters.add(TrajectoryLane.TOOLS)
        view._refresh()

        assert view.select_and_reveal_record("hidden")

        assert view.state.selected_id == "hidden"
        assert view.state.detail_id == "hidden"
        assert view.state.diagnostic_view is DiagnosticView.ALL
        assert view.state.query == ""
        assert not view.state.lane_filters
        assert not view.state.follow_tail
        assert view.state.ledger_page == 1
        assert view.select_and_reveal_record("result")
        assert view.state.selected_id == "call"
        assert view.state.detail_id == "call"
        assert not view.select_and_reveal_record("missing")
