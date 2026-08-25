from __future__ import annotations

from dataclasses import replace

from theater.constants.daemon import (
    BUS_KIND_AGENT_ASSISTANT,
    BUS_KIND_AGENT_TOOL_CALL,
    BUS_KIND_AGENT_TOOL_RESULT,
    BUS_KIND_AGENT_TRANSCRIPT,
)
from theater.daemon.trajectory.observed_timing import (
    ObservationPoint,
    apply_live_observation,
    apply_observation_points,
    observation_points,
    observation_points_for_history,
)
from theater.trajectory import (
    Timing,
    TimingProvenance,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryStatus,
)


def _record(
    record_id: str,
    index: int,
    kind: TrajectoryKind,
    *,
    timing: Timing | None = None,
    status: TrajectoryStatus = TrajectoryStatus.COMPLETED,
) -> TrajectoryRecord:
    lane = {
        TrajectoryKind.TOOL_CALL: TrajectoryLane.TOOLS,
        TrajectoryKind.TOOL_RESULT: TrajectoryLane.TOOLS,
    }.get(kind, TrajectoryLane.MODEL)
    return TrajectoryRecord(
        record_id=record_id,
        revision=0,
        participant_id="p",
        source_epoch="epoch",
        lane=lane,
        kind=kind,
        source="test",
        summary=record_id,
        raw_index=index,
        timing=timing,
        status=status,
    )


class _Store:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def bus_page_for_participant(self, participant_id, *, before_id=None, limit, kinds):
        rows = [
            row
            for row in self.rows
            if participant_id in {row.get("from_id"), row.get("to_id")}
            and row.get("kind") in kinds
            and (before_id is None or row["id"] < before_id)
        ]
        return rows[-limit:]


def test_observation_rows_use_batch_stamp_then_bus_timestamp() -> None:
    points = observation_points(
        (
            {
                "id": 2,
                "ts": 20.0,
                "from_id": "p",
                "kind": BUS_KIND_AGENT_ASSISTANT,
                "payload": {"index": 2},
            },
            {
                "id": 1,
                "ts": 10.0,
                "from_id": "p",
                "kind": BUS_KIND_AGENT_TOOL_CALL,
                "payload": {"index": 1, "observed_at": 9.5},
            },
        )
    )

    assert points == (
        ObservationPoint(1, 1, TrajectoryKind.TOOL_CALL, 9.5),
        ObservationPoint(2, 2, TrajectoryKind.ASSISTANT, 20.0),
    )


def test_historical_observations_require_latest_matching_attachment() -> None:
    rows = [
        {
            "id": 1,
            "ts": 1.0,
            "to_id": "p",
            "kind": BUS_KIND_AGENT_TRANSCRIPT,
            "payload": {"path": "/tmp/old.jsonl"},
        },
        {
            "id": 2,
            "ts": 2.0,
            "from_id": "p",
            "kind": BUS_KIND_AGENT_TOOL_CALL,
            "payload": {"index": 1},
        },
        {
            "id": 3,
            "ts": 3.0,
            "to_id": "p",
            "kind": BUS_KIND_AGENT_TRANSCRIPT,
            "payload": {"path": "/tmp/current.jsonl"},
        },
        {
            "id": 4,
            "ts": 4.0,
            "from_id": "p",
            "kind": BUS_KIND_AGENT_TOOL_RESULT,
            "payload": {"index": 2},
        },
    ]
    store = _Store(rows)

    assert observation_points_for_history(store, "p", "/tmp/old.jsonl") == ()
    assert observation_points_for_history(store, "p", "/tmp/current.jsonl") == (
        ObservationPoint(4, 2, TrajectoryKind.TOOL_RESULT, 4.0),
    )


def test_observation_points_fill_only_matching_missing_timing() -> None:
    source = Timing(start=1.0, provenance=TimingProvenance.SOURCE)
    records = (
        _record("call", 1, TrajectoryKind.TOOL_CALL),
        _record("result", 2, TrajectoryKind.TOOL_RESULT),
        _record("source", 3, TrajectoryKind.ASSISTANT, timing=source),
    )
    projected = apply_observation_points(
        records,
        (
            ObservationPoint(1, 1, TrajectoryKind.TOOL_CALL, 10.0),
            ObservationPoint(2, 2, TrajectoryKind.TOOL_RESULT, 12.0),
            ObservationPoint(3, 3, TrajectoryKind.ASSISTANT, 14.0),
        ),
    )

    assert projected[0].timing == Timing(start=10.0, provenance=TimingProvenance.OBSERVED)
    assert projected[1].timing == Timing(end=12.0, provenance=TimingProvenance.OBSERVED)
    assert projected[2].timing is source


def test_live_observation_retains_first_point_and_closes_mutable_record() -> None:
    running = _record(
        "model",
        1,
        TrajectoryKind.ASSISTANT,
        status=TrajectoryStatus.RUNNING,
    )
    first = apply_live_observation((running,), 10.0, {})[0]
    unchanged = apply_live_observation((running,), 11.0, {first.record_id: first})[0]
    completed = apply_live_observation(
        (replace(running, revision=1, status=TrajectoryStatus.COMPLETED),),
        12.0,
        {first.record_id: first},
    )[0]

    assert first.timing == Timing(start=10.0, provenance=TimingProvenance.OBSERVED)
    assert unchanged.timing == first.timing
    assert completed.timing == Timing(
        start=10.0,
        end=12.0,
        duration_ms=2_000.0,
        provenance=TimingProvenance.OBSERVED,
    )
