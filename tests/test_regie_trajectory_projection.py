"""Derived-state coverage for the Régie trajectory projection."""

from __future__ import annotations

from theater.regie.trajectory.enums import DiagnosticView
from theater.regie.trajectory.projection import TrajectoryViewProjection
from theater.regie.trajectory.state import ParticipantTrajectoryState
from theater.trajectory import (
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryStatus,
)


def record(
    record_id: str,
    index: int,
    *,
    lane: TrajectoryLane = TrajectoryLane.MODEL,
    kind: TrajectoryKind = TrajectoryKind.ASSISTANT,
    call_id: str | None = None,
    source_epoch: str = "epoch",
) -> TrajectoryRecord:
    return TrajectoryRecord(
        record_id=record_id,
        revision=1,
        participant_id="p1",
        source_epoch=source_epoch,
        lane=lane,
        kind=kind,
        source="codex",
        summary=record_id,
        status=TrajectoryStatus.COMPLETED,
        raw_index=index,
        call_id=call_id,
    )


def state_with(*records: TrajectoryRecord, follow_tail: bool = False) -> ParticipantTrajectoryState:
    state = ParticipantTrajectoryState("p1", follow_tail=follow_tail)
    state.upsert(records)
    return state


def test_projection_cache_stays_stable_without_refresh() -> None:
    state = state_with(record("one", 1), record("two", 2))
    projection = TrajectoryViewProjection(state, page_size=2)
    projection.refresh(state, page_size=2)
    key = projection.search_key
    cache_sizes = (len(projection.search_cache.corpus), len(projection.search_cache.query_scores))

    state.hovered_id = "one"

    assert projection.search_result.record_ids == ("one", "two")
    assert projection.search_key == key
    assert (
        len(projection.search_cache.corpus),
        len(projection.search_cache.query_scores),
    ) == cache_sizes


def test_projection_refresh_invalidates_search_when_query_changes() -> None:
    state = state_with(record("alpha", 1), record("beta", 2))
    projection = TrajectoryViewProjection(state, page_size=2)
    projection.refresh(state, page_size=2)
    initial_key = projection.search_key

    state.query = "beta"
    projection.refresh(state, page_size=2)

    assert projection.search_key != initial_key
    assert projection.search_result.record_ids == ("beta",)


def test_projection_clamps_requested_page_into_state() -> None:
    state = state_with(*(record(f"r{index}", index) for index in range(4)))
    state.ledger_page = 99
    projection = TrajectoryViewProjection(state, page_size=2)

    projection.refresh(state, page_size=2)

    assert state.ledger_page == 1
    assert projection.ledger_page.index == 1
    assert projection.visible_ids == ("r2", "r3")


def test_projection_follows_tail_to_its_page() -> None:
    state = state_with(*(record(f"r{index}", index) for index in range(5)), follow_tail=True)
    projection = TrajectoryViewProjection(state, page_size=2)

    projection.refresh(state, page_size=2)

    assert state.ledger_page == 2
    assert projection.visible_ids == ("r4",)
    assert state.selected_id == "r4"


def test_projection_reveals_selected_anchor_page() -> None:
    state = state_with(*(record(f"r{index}", index) for index in range(5)))
    state.select("r3")
    projection = TrajectoryViewProjection(state, page_size=2)

    projection.refresh(state, page_size=2)

    assert state.ledger_page == 1
    assert projection.visible_ids == ("r2", "r3")
    assert state.selected_id == "r3"


def test_projection_maps_tool_members_to_their_logical_row() -> None:
    state = state_with(
        record("call", 1, lane=TrajectoryLane.TOOLS, kind=TrajectoryKind.TOOL_CALL, call_id="one"),
        record(
            "result", 2, lane=TrajectoryLane.TOOLS, kind=TrajectoryKind.TOOL_RESULT, call_id="one"
        ),
    )
    projection = TrajectoryViewProjection(state, page_size=2)

    projection.refresh(state, page_size=2)

    assert projection.search_result.row_ids == ("call",)
    assert projection.logical_row_id("result") == "call"


def test_default_projection_hides_raw_bus_records_from_ledger_and_timeline() -> None:
    state = state_with(
        record("model", 1),
        record(
            "bus:2",
            2,
            lane=TrajectoryLane.THEATER,
            kind=TrajectoryKind.AWAIT_START,
            source_epoch="theater-bus",
        ),
        record("native-theater", 3, lane=TrajectoryLane.THEATER, kind=TrajectoryKind.THEATER_CALL),
    )
    projection = TrajectoryViewProjection(state, page_size=10)

    records = projection.refresh(state, page_size=10)

    assert [item.record_id for item in records] == ["model", "native-theater"]
    assert projection.search_result.record_ids == ("model", "native-theater")

    state.diagnostic_view = DiagnosticView.COORDINATION
    records = projection.refresh(state, page_size=10)

    assert "bus:2" in {item.record_id for item in records}
    assert set(projection.search_result.record_ids) == {"bus:2", "native-theater"}
