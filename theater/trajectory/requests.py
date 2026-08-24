"""Pure request projections from canonical trajectory records."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Self

from theater.constants.trajectory import (
    TRAJECTORY_IDENTIFIER_MAX_BYTES,
    TRAJECTORY_REQUEST_RECORD_LIMIT,
    TRAJECTORY_SOURCE_MAX_BYTES,
)
from theater.trajectory.content import bounded_text
from theater.trajectory.enums import (
    TimingProvenance,
    TrajectoryLane,
    TrajectoryStatus,
    TrajectoryValidationError,
)
from theater.trajectory.grouping import deterministic_record_order
from theater.trajectory.records import Timing, TrajectoryRecord, TrajectoryUsage
from theater.trajectory.validation import (
    boolean,
    enum_value,
    keys,
    mapping,
    sequence,
    string,
    string_or_none,
)


class TrajectoryRequestIdentity(StrEnum):
    SOURCE = "source"
    USAGE = "usage"
    RECORD = "record"


@dataclass(frozen=True, slots=True)
class TrajectoryRequest:
    request_id: str
    participant_id: str
    source_epoch: str
    source: str
    record_ids: tuple[str, ...]
    identity: TrajectoryRequestIdentity
    source_request_id: str | None = None
    turn_id: str | None = None
    step_id: str | None = None
    model: str | None = None
    status: TrajectoryStatus = TrajectoryStatus.UNKNOWN
    timing: Timing | None = None
    usage: TrajectoryUsage | None = None
    records_truncated: bool = False

    def __post_init__(self) -> None:
        for name in ("request_id", "participant_id", "source_epoch"):
            object.__setattr__(
                self,
                name,
                bounded_text(
                    getattr(self, name),
                    max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                    label=f"request.{name}",
                    nonempty=True,
                ),
            )
        object.__setattr__(
            self,
            "source",
            bounded_text(
                self.source,
                max_bytes=TRAJECTORY_SOURCE_MAX_BYTES,
                label="request.source",
                nonempty=True,
            ),
        )
        for name in ("source_request_id", "turn_id", "step_id", "model"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    bounded_text(
                        value,
                        max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                        label=f"request.{name}",
                        nonempty=True,
                    ),
                )
        if not isinstance(self.record_ids, tuple):
            raise TrajectoryValidationError("request.record_ids must be a tuple")
        record_ids = tuple(
            bounded_text(
                value,
                max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                label="request.record_ids[]",
                nonempty=True,
            )
            for value in self.record_ids
        )
        if not record_ids:
            raise TrajectoryValidationError("request.record_ids must not be empty")
        if len(record_ids) > TRAJECTORY_REQUEST_RECORD_LIMIT:
            raise TrajectoryValidationError(
                f"request.record_ids exceeds {TRAJECTORY_REQUEST_RECORD_LIMIT} values"
            )
        if len(set(record_ids)) != len(record_ids):
            raise TrajectoryValidationError("request.record_ids must not repeat a value")
        object.__setattr__(self, "record_ids", record_ids)
        object.__setattr__(
            self,
            "identity",
            enum_value(TrajectoryRequestIdentity, self.identity, "request.identity"),
        )
        object.__setattr__(
            self,
            "status",
            enum_value(TrajectoryStatus, self.status, "request.status"),
        )
        if self.timing is not None and not isinstance(self.timing, Timing):
            raise TrajectoryValidationError("request.timing must be Timing or null")
        if self.usage is not None and not isinstance(self.usage, TrajectoryUsage):
            raise TrajectoryValidationError("request.usage must be TrajectoryUsage or null")
        if type(self.records_truncated) is not bool:
            raise TrajectoryValidationError("request.records_truncated must be a boolean")

    def to_wire(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "participant_id": self.participant_id,
            "source_epoch": self.source_epoch,
            "source": self.source,
            "record_ids": list(self.record_ids),
            "identity": self.identity.value,
            "source_request_id": self.source_request_id,
            "turn_id": self.turn_id,
            "step_id": self.step_id,
            "model": self.model,
            "status": self.status.value,
            "timing": self.timing.to_wire() if self.timing is not None else None,
            "usage": self.usage.to_wire() if self.usage is not None else None,
            "records_truncated": self.records_truncated,
        }

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = mapping(value, "trajectory request")
        keys(
            data,
            required={
                "request_id",
                "participant_id",
                "source_epoch",
                "source",
                "record_ids",
                "identity",
            },
            optional={
                "source_request_id",
                "turn_id",
                "step_id",
                "model",
                "status",
                "timing",
                "usage",
                "records_truncated",
            },
            label="trajectory request",
        )
        return cls(
            request_id=string(data["request_id"], "request.request_id"),
            participant_id=string(data["participant_id"], "request.participant_id"),
            source_epoch=string(data["source_epoch"], "request.source_epoch"),
            source=string(data["source"], "request.source"),
            record_ids=tuple(
                string(item, "request.record_ids[]")
                for item in sequence(data["record_ids"], "request.record_ids")
            ),
            identity=enum_value(TrajectoryRequestIdentity, data["identity"], "request.identity"),
            source_request_id=string_or_none(
                data.get("source_request_id"), "request.source_request_id"
            ),
            turn_id=string_or_none(data.get("turn_id"), "request.turn_id"),
            step_id=string_or_none(data.get("step_id"), "request.step_id"),
            model=string_or_none(data.get("model"), "request.model"),
            status=enum_value(
                TrajectoryStatus,
                data.get("status", TrajectoryStatus.UNKNOWN.value),
                "request.status",
            ),
            timing=Timing.from_wire(data["timing"]) if data.get("timing") is not None else None,
            usage=(
                TrajectoryUsage.from_wire(data["usage"]) if data.get("usage") is not None else None
            ),
            records_truncated=boolean(
                data.get("records_truncated", False), "request.records_truncated"
            ),
        )


_ACTIVE = frozenset({TrajectoryStatus.PENDING, TrajectoryStatus.RUNNING, TrajectoryStatus.PARTIAL})
_TERMINAL = frozenset(
    {
        TrajectoryStatus.COMPLETED,
        TrajectoryStatus.ERROR,
        TrajectoryStatus.INTERRUPTED,
        TrajectoryStatus.TIMEOUT,
        TrajectoryStatus.CANCELLED,
    }
)


def requests_for_records(records: Iterable[TrajectoryRecord]) -> tuple[TrajectoryRequest, ...]:
    """Project explicit or conservative model request membership."""
    groups: list[list[TrajectoryRecord]] = []
    identities: list[TrajectoryRequestIdentity] = []
    source_keys: list[str | None] = []
    shared_positions: dict[tuple[str, str, str], int] = {}
    for record in deterministic_record_order(records):
        key, explicit = _association(record)
        if key is None:
            continue
        if explicit is None:
            groups.append([record])
            identities.append(TrajectoryRequestIdentity.RECORD)
            source_keys.append(None)
            continue
        group_key = (record.participant_id, record.source_epoch, key)
        position = shared_positions.get(group_key)
        if position is None:
            shared_positions[group_key] = len(groups)
            groups.append([record])
            identities.append(
                TrajectoryRequestIdentity.SOURCE if explicit else TrajectoryRequestIdentity.USAGE
            )
            source_keys.append(key)
            continue
        groups[position].append(record)
        if explicit:
            identities[position] = TrajectoryRequestIdentity.SOURCE
    return tuple(
        _request_for_group(
            group,
            identity=identity,
            source_request_id=source_request_id,
        )
        for group, identity, source_request_id in zip(groups, identities, source_keys, strict=True)
    )


def _association(record: TrajectoryRecord) -> tuple[str | None, bool | None]:
    if record.request_id is not None:
        return record.request_id, True
    if record.usage is not None and record.usage.request_id is not None:
        return record.usage.request_id, False
    if record.lane is TrajectoryLane.MODEL and record.usage is not None:
        return record.record_id, None
    return None, None


def _request_for_group(
    records: list[TrajectoryRecord],
    *,
    identity: TrajectoryRequestIdentity,
    source_request_id: str | None,
) -> TrajectoryRequest:
    latest = records[-1]
    record_ids = tuple(record.record_id for record in records)
    retained_ids = record_ids[-TRAJECTORY_REQUEST_RECORD_LIMIT:]
    usage = next((record.usage for record in reversed(records) if record.usage is not None), None)
    status = next(
        (
            record.status
            for record in reversed(records)
            if record.status is not TrajectoryStatus.UNKNOWN
        ),
        TrajectoryStatus.UNKNOWN,
    )
    key = source_request_id if source_request_id is not None else latest.record_id
    return TrajectoryRequest(
        request_id=_request_id(
            latest.participant_id,
            latest.source_epoch,
            "shared" if source_request_id is not None else "record",
            key,
        ),
        participant_id=latest.participant_id,
        source_epoch=latest.source_epoch,
        source=latest.source,
        record_ids=retained_ids,
        identity=identity,
        source_request_id=source_request_id,
        turn_id=_consistent_identity(records, "turn_id"),
        step_id=_consistent_identity(records, "step_id"),
        model=usage.model if usage is not None else None,
        status=status,
        timing=_timing(records, status),
        usage=usage,
        records_truncated=len(record_ids) > TRAJECTORY_REQUEST_RECORD_LIMIT,
    )


def _consistent_identity(records: list[TrajectoryRecord], name: str) -> str | None:
    values = {getattr(record, name) for record in records if getattr(record, name) is not None}
    return next(iter(values)) if len(values) == 1 else None


def _timing(records: list[TrajectoryRecord], status: TrajectoryStatus) -> Timing | None:
    values = [record.timing for record in records if record.timing is not None]
    if not values:
        return None
    starts = [timing.start for timing in values if timing.start is not None]
    start = min(starts) if starts else None
    latest = values[-1]
    if status in _ACTIVE:
        return Timing(start=start, provenance=latest.provenance)
    if status in _TERMINAL:
        ends = [timing.end for timing in values if timing.end is not None]
        end = max(ends) if ends else None
        if start is not None and end is not None and end >= start:
            return Timing(
                start=start,
                end=end,
                duration_ms=max(0.0, (end - start) * 1000),
                provenance=TimingProvenance.DERIVED,
            )
        with_duration = next(
            (timing for timing in reversed(values) if timing.duration_ms is not None),
            None,
        )
        if with_duration is not None:
            return with_duration
        return latest
    return latest


def _request_id(
    participant_id: str,
    source_epoch: str,
    association: str,
    key: str,
) -> str:
    value = f"request:{participant_id}:{source_epoch}:{association}:{key}"
    if len(value.encode("utf-8")) <= TRAJECTORY_IDENTIFIER_MAX_BYTES:
        return value
    return f"request:{sha256(value.encode('utf-8')).hexdigest()}"


__all__ = ["TrajectoryRequest", "TrajectoryRequestIdentity", "requests_for_records"]
