"""Focused trajectory record-location wire tests."""

from __future__ import annotations

import pytest

from theater.constants.trajectory import (
    TRAJECTORY_IDENTIFIER_MAX_BYTES,
    TRAJECTORY_OVERVIEW_SUMMARY_MAX_BYTES,
)
from theater.trajectory.enums import TrajectoryKind, TrajectoryLane, TrajectoryStatus
from theater.trajectory.location import TrajectoryLocation, TrajectoryLocationResolution
from theater.trajectory.records import TrajectoryRecord


def _record(*, participant_id: str = "p", record_id: str = "record") -> TrajectoryRecord:
    return TrajectoryRecord(
        record_id=record_id,
        revision=0,
        participant_id=participant_id,
        source_epoch="epoch",
        lane=TrajectoryLane.MODEL,
        kind=TrajectoryKind.ASSISTANT,
        source="test",
        summary="answer",
        status=TrajectoryStatus.COMPLETED,
    )


def test_exact_location_round_trips_and_requires_matching_record() -> None:
    record = _record()
    location = TrajectoryLocation("p", "record", TrajectoryLocationResolution.EXACT, record)

    assert TrajectoryLocation.from_wire(location.to_wire()) == location
    with pytest.raises(ValueError, match="must match participant and requested record"):
        TrajectoryLocation("other", "record", TrajectoryLocationResolution.EXACT, record)
    with pytest.raises(ValueError, match="requires a record"):
        TrajectoryLocation("p", "record", TrajectoryLocationResolution.EXACT)


def test_non_exact_location_has_no_record_and_wire_is_strict_and_bounded() -> None:
    record = _record()
    with pytest.raises(ValueError, match="must not contain a record"):
        TrajectoryLocation("p", "record", TrajectoryLocationResolution.NOT_FOUND, record)
    with pytest.raises(ValueError):
        TrajectoryLocation(
            "p",
            "record",
            TrajectoryLocationResolution.UNAVAILABLE,
            message="x" * (TRAJECTORY_OVERVIEW_SUMMARY_MAX_BYTES + 1),
        )
    with pytest.raises(ValueError):
        TrajectoryLocation(
            "x" * (TRAJECTORY_IDENTIFIER_MAX_BYTES + 1),
            "record",
            TrajectoryLocationResolution.NOT_FOUND,
        )
    with pytest.raises(ValueError):
        TrajectoryLocation.from_wire(
            {
                "participant_id": "p",
                "requested_record_id": "record",
                "resolution": "not_found",
                "record": None,
                "message": "missing",
                "extra": True,
            }
        )
