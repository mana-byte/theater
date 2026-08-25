"""Pure request projections from canonical trajectory records."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
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
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryStatus,
    TrajectoryValidationError,
)
from theater.trajectory.grouping import deterministic_record_order
from theater.trajectory.records import Timing, TrajectoryFailure, TrajectoryRecord, TrajectoryUsage
from theater.trajectory.timing import derived_interval, terminal_timestamp
from theater.trajectory.validation import (
    boolean,
    enum_value,
    integer,
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
    provider: str | None = None
    status: TrajectoryStatus = TrajectoryStatus.UNKNOWN
    timing: Timing | None = None
    usage: TrajectoryUsage | None = None
    failure: TrajectoryFailure | None = None
    retry_of_record_id: str | None = None
    retry_attempt: int | None = None
    context_record_ids: tuple[str, ...] = ()
    model_record_ids: tuple[str, ...] = ()
    tool_record_ids: tuple[str, ...] = ()
    coordination_record_ids: tuple[str, ...] = ()
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
        for name in (
            "source_request_id",
            "turn_id",
            "step_id",
            "model",
            "provider",
            "retry_of_record_id",
        ):
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
        _validate_request_payload(self)
        for name in (
            "context_record_ids",
            "model_record_ids",
            "tool_record_ids",
            "coordination_record_ids",
        ):
            values = _record_id_subset(getattr(self, name), self.record_ids, f"request.{name}")
            object.__setattr__(self, name, values)
        if type(self.records_truncated) is not bool:
            raise TrajectoryValidationError("request.records_truncated must be a boolean")

    @property
    def ttft_ms(self) -> float | None:
        return self.timing.ttft_ms if self.timing is not None else None

    @property
    def generation_duration_ms(self) -> float | None:
        return self.timing.generation_duration_ms if self.timing is not None else None

    @property
    def output_tokens_per_second(self) -> float | None:
        duration = self.generation_duration_ms
        if self.usage is None or duration is None or duration <= 0:
            return None
        return self.usage.output_tokens * 1000 / duration

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
            "provider": self.provider,
            "status": self.status.value,
            "timing": self.timing.to_wire() if self.timing is not None else None,
            "usage": self.usage.to_wire() if self.usage is not None else None,
            "failure": self.failure.to_wire() if self.failure is not None else None,
            "retry_of_record_id": self.retry_of_record_id,
            "retry_attempt": self.retry_attempt,
            "context_record_ids": list(self.context_record_ids),
            "model_record_ids": list(self.model_record_ids),
            "tool_record_ids": list(self.tool_record_ids),
            "coordination_record_ids": list(self.coordination_record_ids),
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
                "provider",
                "status",
                "timing",
                "usage",
                "failure",
                "retry_of_record_id",
                "retry_attempt",
                "context_record_ids",
                "model_record_ids",
                "tool_record_ids",
                "coordination_record_ids",
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
            provider=string_or_none(data.get("provider"), "request.provider"),
            status=enum_value(
                TrajectoryStatus,
                data.get("status", TrajectoryStatus.UNKNOWN.value),
                "request.status",
            ),
            timing=Timing.from_wire(data["timing"]) if data.get("timing") is not None else None,
            usage=(
                TrajectoryUsage.from_wire(data["usage"]) if data.get("usage") is not None else None
            ),
            failure=(
                TrajectoryFailure.from_wire(data["failure"])
                if data.get("failure") is not None
                else None
            ),
            retry_of_record_id=string_or_none(
                data.get("retry_of_record_id"), "request.retry_of_record_id"
            ),
            retry_attempt=(
                integer(data["retry_attempt"], "request.retry_attempt")
                if data.get("retry_attempt") is not None
                else None
            ),
            context_record_ids=_wire_record_ids(data, "context_record_ids"),
            model_record_ids=_wire_record_ids(data, "model_record_ids"),
            tool_record_ids=_wire_record_ids(data, "tool_record_ids"),
            coordination_record_ids=_wire_record_ids(data, "coordination_record_ids"),
            records_truncated=boolean(
                data.get("records_truncated", False), "request.records_truncated"
            ),
        )


def _validate_request_payload(request: TrajectoryRequest) -> None:
    if request.timing is not None and not isinstance(request.timing, Timing):
        raise TrajectoryValidationError("request.timing must be Timing or null")
    if request.usage is not None and not isinstance(request.usage, TrajectoryUsage):
        raise TrajectoryValidationError("request.usage must be TrajectoryUsage or null")
    if request.failure is not None and not isinstance(request.failure, TrajectoryFailure):
        raise TrajectoryValidationError("request.failure must be TrajectoryFailure or null")
    if request.retry_attempt is not None and request.retry_of_record_id is None:
        raise TrajectoryValidationError("request retry attempt requires a retry link")
    if request.retry_attempt is not None and (
        type(request.retry_attempt) is not int or request.retry_attempt <= 0
    ):
        raise TrajectoryValidationError("request.retry_attempt must be a positive integer")


def _record_id_subset(
    values: object,
    record_ids: tuple[str, ...],
    label: str,
) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise TrajectoryValidationError(f"{label} must be a tuple")
    bounded = tuple(
        bounded_text(
            value,
            max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
            label=f"{label}[]",
            nonempty=True,
        )
        for value in values
    )
    if len(bounded) > TRAJECTORY_REQUEST_RECORD_LIMIT or len(set(bounded)) != len(bounded):
        raise TrajectoryValidationError(f"{label} must contain unique bounded values")
    if not set(bounded).issubset(record_ids):
        raise TrajectoryValidationError(f"{label} must reference retained request records")
    return bounded


def _wire_record_ids(data: Mapping[str, object], name: str) -> tuple[str, ...]:
    return tuple(
        string(item, f"request.{name}[]")
        for item in sequence(data.get(name, []), f"request.{name}")
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
    request_records = _request_records(records)
    record_ids = tuple(record.record_id for record in records)
    retained_ids = record_ids[-TRAJECTORY_REQUEST_RECORD_LIMIT:]
    usage = next(
        (record.usage for record in reversed(request_records) if record.usage is not None),
        None,
    )
    status = next(
        (
            record.status
            for record in reversed(request_records)
            if record.status is not TrajectoryStatus.UNKNOWN
        ),
        TrajectoryStatus.UNKNOWN,
    )
    retry = next(
        (record for record in reversed(request_records) if record.retry_of_record_id is not None),
        None,
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
        provider=usage.provider if usage is not None else None,
        status=status,
        timing=_timing(request_records, status),
        usage=usage,
        failure=next(
            (record.failure for record in reversed(request_records) if record.failure),
            None,
        ),
        retry_of_record_id=retry.retry_of_record_id if retry is not None else None,
        retry_attempt=retry.retry_attempt if retry is not None else None,
        context_record_ids=_associated_ids(records, retained_ids, kind=TrajectoryKind.CONTEXT),
        model_record_ids=_associated_ids(records, retained_ids, lane=TrajectoryLane.MODEL),
        tool_record_ids=_associated_ids(records, retained_ids, lane=TrajectoryLane.TOOLS),
        coordination_record_ids=_associated_ids(records, retained_ids, lane=TrajectoryLane.THEATER),
        records_truncated=len(record_ids) > TRAJECTORY_REQUEST_RECORD_LIMIT,
    )


def _request_records(records: list[TrajectoryRecord]) -> list[TrajectoryRecord]:
    model_records = [
        record
        for record in records
        if record.lane is TrajectoryLane.MODEL and record.kind is not TrajectoryKind.CONTEXT
    ]
    return model_records or records


def _associated_ids(
    records: list[TrajectoryRecord],
    retained_ids: tuple[str, ...],
    *,
    lane: TrajectoryLane | None = None,
    kind: TrajectoryKind | None = None,
) -> tuple[str, ...]:
    retained = set(retained_ids)
    return tuple(
        record.record_id
        for record in records
        if record.record_id in retained
        and (kind is None or record.kind is kind)
        and (lane is None or record.lane is lane)
        and not (lane is TrajectoryLane.MODEL and record.kind is TrajectoryKind.CONTEXT)
    )


def _consistent_identity(records: list[TrajectoryRecord], name: str) -> str | None:
    values = {getattr(record, name) for record in records if getattr(record, name) is not None}
    return next(iter(values)) if len(values) == 1 else None


def _timing(records: list[TrajectoryRecord], status: TrajectoryStatus) -> Timing | None:
    timed_records = [record for record in records if record.timing is not None]
    values = [record.timing for record in timed_records if record.timing is not None]
    if not values:
        return None
    starts = [timing for timing in values if timing.start is not None]
    start_timing = min(starts, key=lambda timing: timing.start or 0) if starts else None
    start = start_timing.start if start_timing is not None else None
    latest = values[-1]
    first_tokens = [
        timing.first_token
        for timing in values
        if timing.first_token is not None and (start is None or timing.first_token >= start)
    ]
    first_token = min(first_tokens) if first_tokens else None
    if status in _ACTIVE:
        provenance = start_timing.provenance if start_timing is not None else latest.provenance
        return Timing(start=start, first_token=first_token, provenance=provenance)
    if status in _TERMINAL:
        ends = [
            record.timing
            for record in timed_records
            if record.status in _TERMINAL
            and record.timing is not None
            and terminal_timestamp(record.timing) is not None
        ]
        end_timing = max(ends, key=lambda timing: terminal_timestamp(timing) or 0) if ends else None
        if start_timing is not None and end_timing is not None:
            interval = derived_interval(start_timing, end_timing, first_token=first_token)
            if interval is not None:
                return interval
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
