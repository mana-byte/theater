"""A trajectory record builder for the capability and overview tests."""

from __future__ import annotations

from theater.trajectory import (
    Timing,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryStatus,
    TrajectoryUsage,
)


def overview_record(
    record_id: str,
    index: int,
    *,
    source_epoch: str = "epoch",
    kind: TrajectoryKind = TrajectoryKind.ASSISTANT,
    lane: TrajectoryLane = TrajectoryLane.MODEL,
    status: TrajectoryStatus = TrajectoryStatus.COMPLETED,
    call_id: str | None = None,
    request_id: str | None = None,
    usage: TrajectoryUsage | None = None,
    timing: Timing | None = None,
    retry_of_record_id: str | None = None,
    retry_attempt: int | None = None,
) -> TrajectoryRecord:
    return TrajectoryRecord(
        record_id=record_id,
        revision=0,
        participant_id="participant",
        source_epoch=source_epoch,
        lane=lane,
        kind=kind,
        source="test",
        summary=record_id,
        status=status,
        raw_index=index,
        call_id=call_id,
        request_id=request_id,
        usage=usage,
        timing=timing,
        retry_of_record_id=retry_of_record_id,
        retry_attempt=retry_attempt,
    )
