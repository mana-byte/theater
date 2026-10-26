"""Exact daemon-side record merge and presentation ordering helpers."""

from __future__ import annotations

from collections.abc import Iterable

from theater.constants.trajectory import TRAJECTORY_THEATER_BUS_RECORD_PREFIX
from theater.trajectory import (
    TrajectoryGroup,
    TrajectoryKind,
    TrajectoryRecord,
    TrajectoryStatus,
    deterministic_record_order,
    group_records,
)

_INTERACTION_KINDS = frozenset(
    {
        TrajectoryKind.SPAWN,
        TrajectoryKind.RESUME,
        TrajectoryKind.SEND,
        TrajectoryKind.RECEIVE,
        TrajectoryKind.AWAIT_START,
        TrajectoryKind.AWAIT_END,
        TrajectoryKind.KILL,
        TrajectoryKind.THEATER_CALL,
        TrajectoryKind.THEATER_RESULT,
        TrajectoryKind.JOB_FAILURE,
        TrajectoryKind.TRANSCRIPT_BOUNDARY,
        TrajectoryKind.SESSION_BOUNDARY,
        TrajectoryKind.OBSERVATION_ERROR,
    }
)


def order_records(records: Iterable[TrajectoryRecord]) -> tuple[TrajectoryRecord, ...]:
    """Order records with the canonical cross-stream chronology policy."""
    return deterministic_record_order(records)


def groups_for_records(records: Iterable[TrajectoryRecord]) -> tuple[TrajectoryGroup, ...]:
    """Build canonical turn, step, and between-turn groups."""
    return group_records(records)


def is_interaction(record: TrajectoryRecord) -> bool:
    """Whether an update must wake followers without the coalescing delay."""
    return (
        record.record_id.startswith(TRAJECTORY_THEATER_BUS_RECORD_PREFIX)
        or record.kind in _INTERACTION_KINDS
    )


def is_mutable(record: TrajectoryRecord, previous: TrajectoryRecord | None) -> bool:
    """Whether a non-Theater record may be coalesced for one render interval."""
    if is_interaction(record):
        return False
    if previous is not None:
        return previous.status in {TrajectoryStatus.RUNNING, TrajectoryStatus.PARTIAL}
    return record.status in {TrajectoryStatus.RUNNING, TrajectoryStatus.PARTIAL}


__all__ = [
    "groups_for_records",
    "is_interaction",
    "is_mutable",
    "order_records",
]
