"""Pure derived-state coverage for the Régie trajectory projection."""

from __future__ import annotations

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
