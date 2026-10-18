"""A minimal canonical trajectory record builder."""

from __future__ import annotations

from theater.trajectory import (
    DetailField,
    Timing,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryStatus,
)


def make_record(
    record_id: str,
    *,
    revision: int = 0,
    raw_index: int = 0,
    source_epoch: str = "epoch",
    turn_id: str | None = None,
    step_id: str | None = None,
    details: tuple[DetailField, ...] = (),
    timing: Timing | None = None,
    call_id: str | None = None,
    parent_call_id: str | None = None,
    retry_of_record_id: str | None = None,
    retry_attempt: int | None = None,
) -> TrajectoryRecord:
    return TrajectoryRecord(
        record_id=record_id,
        revision=revision,
        participant_id="participant",
        source_epoch=source_epoch,
        lane=TrajectoryLane.MODEL,
        kind=TrajectoryKind.ASSISTANT,
        source="baseline",
        summary="answer",
        status=TrajectoryStatus.COMPLETED,
        raw_index=raw_index,
        turn_id=turn_id,
        step_id=step_id,
        timing=timing,
        call_id=call_id,
        parent_call_id=parent_call_id,
        retry_of_record_id=retry_of_record_id,
        retry_attempt=retry_attempt,
        details=details,
    )
