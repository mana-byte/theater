"""Strict bounded result for exact trajectory record lookup."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from theater.constants.trajectory import (
    TRAJECTORY_IDENTIFIER_MAX_BYTES,
    TRAJECTORY_OVERVIEW_SUMMARY_MAX_BYTES,
)
from theater.trajectory.content import bounded_text
from theater.trajectory.enums import TrajectoryValidationError
from theater.trajectory.records import TrajectoryRecord
from theater.trajectory.validation import enum_value, keys, mapping, string


class TrajectoryLocationResolution(StrEnum):
    EXACT = "exact"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class TrajectoryLocation:
    participant_id: str
    requested_record_id: str
    resolution: TrajectoryLocationResolution
    record: TrajectoryRecord | None = None
    message: str = ""

    def __post_init__(self) -> None:
        for name in ("participant_id", "requested_record_id"):
            object.__setattr__(
                self,
                name,
                bounded_text(
                    getattr(self, name),
                    max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                    label=f"trajectory location {name}",
                    nonempty=True,
                ),
            )
        object.__setattr__(
            self,
            "resolution",
            enum_value(
                TrajectoryLocationResolution, self.resolution, "trajectory location resolution"
            ),
        )
        object.__setattr__(
            self,
            "message",
            bounded_text(
                self.message,
                max_bytes=TRAJECTORY_OVERVIEW_SUMMARY_MAX_BYTES,
                label="trajectory location message",
            ),
        )
        if self.resolution is TrajectoryLocationResolution.EXACT:
            if not isinstance(self.record, TrajectoryRecord):
                raise TrajectoryValidationError("exact trajectory location requires a record")
            if (
                self.record.participant_id != self.participant_id
                or self.record.record_id != self.requested_record_id
            ):
                raise TrajectoryValidationError(
                    "exact trajectory location record must match participant and requested record"
                )
        elif self.record is not None:
            raise TrajectoryValidationError(
                "non-exact trajectory location must not contain a record"
            )

    def to_wire(self) -> dict[str, object]:
        return {
            "participant_id": self.participant_id,
            "requested_record_id": self.requested_record_id,
            "resolution": self.resolution.value,
            "record": self.record.to_wire() if self.record is not None else None,
            "message": self.message,
        }

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = mapping(value, "trajectory location")
        keys(
            data,
            required={"participant_id", "requested_record_id", "resolution", "record", "message"},
            optional=set(),
            label="trajectory location",
        )
        record_value = data["record"]
        return cls(
            participant_id=string(data["participant_id"], "trajectory location participant_id"),
            requested_record_id=string(
                data["requested_record_id"], "trajectory location requested_record_id"
            ),
            resolution=enum_value(
                TrajectoryLocationResolution,
                data["resolution"],
                "trajectory location resolution",
            ),
            record=TrajectoryRecord.from_wire(record_value) if record_value is not None else None,
            message=string(data["message"], "trajectory location message"),
        )


__all__ = ["TrajectoryLocation", "TrajectoryLocationResolution"]
