"""Pure exact tool operation projections from canonical trajectory records."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Self

from theater.constants.trajectory import (
    TRAJECTORY_IDENTIFIER_MAX_BYTES,
    TRAJECTORY_SOURCE_MAX_BYTES,
    TRAJECTORY_TOOL_RECORD_LIMIT,
)
from theater.trajectory.content import (
    ContentPreview,
    DetailField,
    bound_detail_fields,
    bounded_text,
)
from theater.trajectory.enums import (
    TrajectoryKind,
    TrajectoryStatus,
    TrajectoryValidationError,
)
from theater.trajectory.grouping import deterministic_record_order
from theater.trajectory.records import Timing, TrajectoryFailure, TrajectoryRecord
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


class TrajectoryToolIdentity(StrEnum):
    MATCHED = "matched"
    CALL_ONLY = "call_only"
    RESULT_ONLY = "result_only"
    UNKEYED_CALL = "unkeyed_call"
    UNKEYED_RESULT = "unkeyed_result"


_TERMINAL = frozenset(
    {
        TrajectoryStatus.COMPLETED,
        TrajectoryStatus.ERROR,
        TrajectoryStatus.INTERRUPTED,
        TrajectoryStatus.TIMEOUT,
        TrajectoryStatus.CANCELLED,
    }
)


@dataclass(frozen=True, slots=True)
class TrajectoryToolOperation:
    operation_id: str
    participant_id: str
    source_epoch: str
    source: str
    identity: TrajectoryToolIdentity
    call_id: str | None
    call_record_ids: tuple[str, ...]
    result_record_ids: tuple[str, ...]
    tool_name: str | None
    status: TrajectoryStatus
    mcp_server: str | None = None
    mcp_tool: str | None = None
    timing: Timing | None = None
    request_id: str | None = None
    parent_call_id: str | None = None
    child_call_ids: tuple[str, ...] = ()
    call_details: tuple[DetailField, ...] = ()
    result_details: tuple[DetailField, ...] = ()
    call_count: int = 0
    result_count: int = 0
    records_truncated: bool = False
    failure: TrajectoryFailure | None = None
    retry_of_record_id: str | None = None
    retry_attempt: int | None = None

    def __post_init__(self) -> None:
        _bound_attributes(self)
        _validate_record_ids(self)
        _validate_counts(self)
        self._validate_identity()

    def _validate_identity(self) -> None:
        keyed = self.call_id is not None
        calls = self.call_count > 0
        results = self.result_count > 0
        expected = (
            TrajectoryToolIdentity.MATCHED
            if keyed and calls and results
            else TrajectoryToolIdentity.CALL_ONLY
            if keyed and calls
            else TrajectoryToolIdentity.RESULT_ONLY
            if keyed and results
            else TrajectoryToolIdentity.UNKEYED_CALL
            if not keyed and calls
            else TrajectoryToolIdentity.UNKEYED_RESULT
            if not keyed and results
            else None
        )
        if self.identity is not expected:
            raise TrajectoryValidationError("tool.identity does not match call/result records")
        if self.identity is TrajectoryToolIdentity.UNKEYED_CALL and (
            self.call_count != 1 or self.result_count != 0
        ):
            raise TrajectoryValidationError("unkeyed call operations contain exactly one call")
        if self.identity is TrajectoryToolIdentity.UNKEYED_RESULT and (
            self.result_count != 1 or self.call_count != 0
        ):
            raise TrajectoryValidationError("unkeyed result operations contain exactly one result")

    def to_wire(self) -> dict[str, object]:
        value: dict[str, object] = {
            "operation_id": self.operation_id,
            "participant_id": self.participant_id,
            "source_epoch": self.source_epoch,
            "source": self.source,
            "identity": self.identity.value,
            "call_id": self.call_id,
            "call_record_ids": list(self.call_record_ids),
            "result_record_ids": list(self.result_record_ids),
            "tool_name": self.tool_name,
            "status": self.status.value,
            "timing": self.timing.to_wire() if self.timing is not None else None,
            "request_id": self.request_id,
            "parent_call_id": self.parent_call_id,
            "child_call_ids": list(self.child_call_ids),
            "call_details": [detail.to_wire() for detail in self.call_details],
            "result_details": [detail.to_wire() for detail in self.result_details],
            "call_count": self.call_count,
            "result_count": self.result_count,
            "records_truncated": self.records_truncated,
        }
        if self.mcp_server is not None:
            value["mcp_server"] = self.mcp_server
            value["mcp_tool"] = self.mcp_tool
        if self.failure is not None:
            value["failure"] = self.failure.to_wire()
        if self.retry_of_record_id is not None:
            value["retry_of_record_id"] = self.retry_of_record_id
            value["retry_attempt"] = self.retry_attempt
        return value

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = mapping(value, "trajectory tool operation")
        required = {
            "operation_id",
            "participant_id",
            "source_epoch",
            "source",
            "identity",
            "call_id",
            "call_record_ids",
            "result_record_ids",
            "tool_name",
            "status",
            "timing",
            "request_id",
            "parent_call_id",
            "child_call_ids",
            "call_details",
            "result_details",
            "call_count",
            "result_count",
            "records_truncated",
        }
        keys(
            data,
            required=required,
            optional={
                "failure",
                "mcp_server",
                "mcp_tool",
                "retry_of_record_id",
                "retry_attempt",
            },
            label="trajectory tool operation",
        )
        return cls(
            operation_id=string(data["operation_id"], "tool.operation_id"),
            participant_id=string(data["participant_id"], "tool.participant_id"),
            source_epoch=string(data["source_epoch"], "tool.source_epoch"),
            source=string(data["source"], "tool.source"),
            identity=enum_value(TrajectoryToolIdentity, data["identity"], "tool.identity"),
            call_id=string_or_none(data["call_id"], "tool.call_id"),
            call_record_ids=tuple(
                string(item, "tool.call_record_ids[]")
                for item in sequence(data["call_record_ids"], "tool.call_record_ids")
            ),
            result_record_ids=tuple(
                string(item, "tool.result_record_ids[]")
                for item in sequence(data["result_record_ids"], "tool.result_record_ids")
            ),
            tool_name=string_or_none(data["tool_name"], "tool.tool_name"),
            status=enum_value(TrajectoryStatus, data["status"], "tool.status"),
            mcp_server=string_or_none(data.get("mcp_server"), "tool.mcp_server"),
            mcp_tool=string_or_none(data.get("mcp_tool"), "tool.mcp_tool"),
            timing=Timing.from_wire(data["timing"]) if data["timing"] is not None else None,
            request_id=string_or_none(data["request_id"], "tool.request_id"),
            parent_call_id=string_or_none(data["parent_call_id"], "tool.parent_call_id"),
            child_call_ids=tuple(
                string(item, "tool.child_call_ids[]")
                for item in sequence(data["child_call_ids"], "tool.child_call_ids")
            ),
            call_details=tuple(
                DetailField.from_wire(item)
                for item in sequence(data["call_details"], "tool.call_details")
            ),
            result_details=tuple(
                DetailField.from_wire(item)
                for item in sequence(data["result_details"], "tool.result_details")
            ),
            call_count=integer(data["call_count"], "tool.call_count"),
            result_count=integer(data["result_count"], "tool.result_count"),
            records_truncated=boolean(data["records_truncated"], "tool.records_truncated"),
            failure=(
                TrajectoryFailure.from_wire(data["failure"])
                if data.get("failure") is not None
                else None
            ),
            retry_of_record_id=string_or_none(
                data.get("retry_of_record_id"), "tool.retry_of_record_id"
            ),
            retry_attempt=(
                integer(data["retry_attempt"], "tool.retry_attempt")
                if data.get("retry_attempt") is not None
                else None
            ),
        )


def tool_operations_for_records(
    records: Iterable[TrajectoryRecord],
) -> tuple[TrajectoryToolOperation, ...]:
    """Project exact tool-call and tool-result associations."""
    groups: list[list[TrajectoryRecord]] = []
    positions: dict[tuple[str, str, str], int] = {}
    child_ids: dict[tuple[str, str, str], list[str]] = {}
    child_seen: dict[tuple[str, str, str], set[str]] = {}
    ordered = deterministic_record_order(records)
    for record in ordered:
        if (
            record.kind is TrajectoryKind.TOOL_CALL
            and record.call_id is not None
            and record.parent_call_id is not None
        ):
            parent_key = (record.participant_id, record.source_epoch, record.parent_call_id)
            seen = child_seen.setdefault(parent_key, set())
            if record.call_id not in seen:
                seen.add(record.call_id)
                child_ids.setdefault(parent_key, []).append(record.call_id)
        if record.kind not in {TrajectoryKind.TOOL_CALL, TrajectoryKind.TOOL_RESULT}:
            continue
        if record.call_id is None:
            groups.append([record])
            continue
        key = (record.participant_id, record.source_epoch, record.call_id)
        position = positions.get(key)
        if position is None:
            positions[key] = len(groups)
            groups.append([record])
        else:
            groups[position].append(record)
    child_links = {key: tuple(value) for key, value in child_ids.items()}
    return tuple(_operation_for_group(group, child_links) for group in groups)


def _operation_for_group(
    records: list[TrajectoryRecord], child_links: dict[tuple[str, str, str], tuple[str, ...]]
) -> TrajectoryToolOperation:
    calls = [record for record in records if record.kind is TrajectoryKind.TOOL_CALL]
    results = [record for record in records if record.kind is TrajectoryKind.TOOL_RESULT]
    primary_call = calls[-1] if calls else None
    primary_result = results[-1] if results else None
    primary = primary_call or primary_result
    assert primary is not None
    display = primary_result or primary_call
    assert display is not None
    call_id = primary.call_id
    call_ids = tuple(record.record_id for record in calls)
    result_ids = tuple(record.record_id for record in results)
    mcp_server, mcp_tool = _mcp_identity(records)
    return TrajectoryToolOperation(
        operation_id=_operation_id(primary.participant_id, primary.source_epoch, call_id, primary),
        participant_id=primary.participant_id,
        source_epoch=primary.source_epoch,
        source=primary.source,
        identity=_identity(call_id, calls, results),
        call_id=call_id,
        call_record_ids=call_ids[-TRAJECTORY_TOOL_RECORD_LIMIT:],
        result_record_ids=result_ids[-TRAJECTORY_TOOL_RECORD_LIMIT:],
        tool_name=_tool_name(primary_call),
        status=display.status,
        mcp_server=mcp_server,
        mcp_tool=mcp_tool,
        timing=_timing(calls, results, primary_call, primary_result),
        request_id=_consistent(records, "request_id"),
        parent_call_id=_parent_call_id(calls, results, call_id),
        child_call_ids=_child_call_ids(
            child_links, primary.participant_id, primary.source_epoch, call_id
        ),
        call_details=primary_call.details if primary_call is not None else (),
        result_details=primary_result.details if primary_result is not None else (),
        call_count=len(calls),
        result_count=len(results),
        records_truncated=(
            len(call_ids) > TRAJECTORY_TOOL_RECORD_LIMIT
            or len(result_ids) > TRAJECTORY_TOOL_RECORD_LIMIT
        ),
        failure=next((record.failure for record in reversed(records) if record.failure), None),
        retry_of_record_id=next(
            (
                record.retry_of_record_id
                for record in reversed(records)
                if record.retry_of_record_id is not None
            ),
            None,
        ),
        retry_attempt=next(
            (
                record.retry_attempt
                for record in reversed(records)
                if record.retry_of_record_id is not None
            ),
            None,
        ),
    )


def _identity(
    call_id: str | None, calls: list[TrajectoryRecord], results: list[TrajectoryRecord]
) -> TrajectoryToolIdentity:
    if call_id is None:
        return (
            TrajectoryToolIdentity.UNKEYED_CALL if calls else TrajectoryToolIdentity.UNKEYED_RESULT
        )
    if calls and results:
        return TrajectoryToolIdentity.MATCHED
    return TrajectoryToolIdentity.CALL_ONLY if calls else TrajectoryToolIdentity.RESULT_ONLY


def _operation_id(
    participant_id: str, source_epoch: str, call_id: str | None, record: TrajectoryRecord
) -> str:
    suffix = f"call:{call_id}" if call_id is not None else f"{record.kind.value}:{record.record_id}"
    value = f"tool:{participant_id}:{source_epoch}:{suffix}"
    if len(value.encode("utf-8")) <= TRAJECTORY_IDENTIFIER_MAX_BYTES:
        return value
    return f"tool:{sha256(value.encode('utf-8')).hexdigest()}"


def _tool_name(record: TrajectoryRecord | None) -> str | None:
    if record is None:
        return None
    detail = next(
        (
            field.preview.text
            for field in reversed(record.details)
            if field.name == "tool" and field.preview.text
        ),
        None,
    )
    value = detail or record.summary or None
    return (
        ContentPreview.from_text(value, max_bytes=TRAJECTORY_SOURCE_MAX_BYTES).text
        if value is not None
        else None
    )


def _consistent(records: list[TrajectoryRecord], name: str) -> str | None:
    values = {getattr(record, name) for record in records if getattr(record, name) is not None}
    return next(iter(values)) if len(values) == 1 else None


def _mcp_identity(records: list[TrajectoryRecord]) -> tuple[str | None, str | None]:
    identities = {
        (record.mcp_server, record.mcp_tool)
        for record in records
        if record.mcp_server is not None and record.mcp_tool is not None
    }
    return next(iter(identities)) if len(identities) == 1 else (None, None)


def _parent_call_id(
    calls: list[TrajectoryRecord], results: list[TrajectoryRecord], call_id: str | None
) -> str | None:
    parent = _consistent(calls, "parent_call_id")
    if parent is not None or any(record.parent_call_id is not None for record in calls):
        return parent
    parents = [
        record
        for record in results
        if record.parent_call_id is not None and record.parent_call_id != call_id
    ]
    return _consistent(parents, "parent_call_id")


def _child_call_ids(
    child_links: dict[tuple[str, str, str], tuple[str, ...]],
    participant_id: str,
    source_epoch: str,
    call_id: str | None,
) -> tuple[str, ...]:
    if call_id is None:
        return ()
    children = child_links.get((participant_id, source_epoch, call_id), ())
    return tuple(child for child in children if child != call_id)[-TRAJECTORY_TOOL_RECORD_LIMIT:]


def _timing(
    calls: list[TrajectoryRecord],
    results: list[TrajectoryRecord],
    primary_call: TrajectoryRecord | None,
    primary_result: TrajectoryRecord | None,
) -> Timing | None:
    starts: list[Timing] = []
    for record in calls:
        timing = record.timing
        if timing is not None and timing.start is not None:
            starts.append(timing)
    start_timing = (
        min(starts, key=lambda timing: timing.start if timing.start is not None else 0)
        if starts
        else None
    )
    start = start_timing.start if start_timing is not None else None
    end_records: list[TrajectoryRecord] = []
    if primary_result is not None and primary_result.status in _TERMINAL:
        end_records = results
    elif primary_result is None and primary_call is not None and primary_call.status in _TERMINAL:
        end_records = calls
    ends: list[Timing] = []
    for record in end_records:
        timing = record.timing
        if timing is not None and terminal_timestamp(timing) is not None:
            ends.append(timing)
    end_timing = max(ends, key=lambda timing: terminal_timestamp(timing) or 0) if ends else None
    end = terminal_timestamp(end_timing) if end_timing is not None else None
    if start_timing is not None and end_timing is not None:
        interval = derived_interval(start_timing, end_timing)
        if interval is not None:
            return interval
    for candidate in (primary_result, primary_call):
        if (
            candidate is not None
            and candidate.timing is not None
            and candidate.timing.duration_ms is not None
        ):
            return Timing(
                start=start,
                duration_ms=candidate.timing.duration_ms,
                provenance=candidate.timing.provenance,
            )
    if start is not None:
        assert start_timing is not None
        return Timing(start=start, provenance=start_timing.provenance)
    if end is not None:
        assert end_timing is not None
        return Timing(end=end, provenance=end_timing.provenance)
    return None


def _bound_attributes(operation: TrajectoryToolOperation) -> None:
    for name in ("operation_id", "participant_id", "source_epoch"):
        object.__setattr__(
            operation,
            name,
            bounded_text(
                getattr(operation, name),
                max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                label=f"tool.{name}",
                nonempty=True,
            ),
        )
    object.__setattr__(
        operation,
        "source",
        bounded_text(
            operation.source,
            max_bytes=TRAJECTORY_SOURCE_MAX_BYTES,
            label="tool.source",
            nonempty=True,
        ),
    )
    for name in (
        "call_id",
        "request_id",
        "parent_call_id",
        "mcp_server",
        "mcp_tool",
        "retry_of_record_id",
    ):
        value = getattr(operation, name)
        if value is not None:
            object.__setattr__(
                operation,
                name,
                bounded_text(
                    value,
                    max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                    label=f"tool.{name}",
                    nonempty=True,
                ),
            )
    if (operation.mcp_server is None) != (operation.mcp_tool is None):
        raise TrajectoryValidationError("tool MCP identity requires both server and tool")
    if operation.tool_name is not None:
        object.__setattr__(
            operation,
            "tool_name",
            bounded_text(
                operation.tool_name,
                max_bytes=TRAJECTORY_SOURCE_MAX_BYTES,
                label="tool.tool_name",
                nonempty=True,
            ),
        )
    object.__setattr__(
        operation,
        "identity",
        enum_value(TrajectoryToolIdentity, operation.identity, "tool.identity"),
    )
    object.__setattr__(
        operation,
        "status",
        enum_value(TrajectoryStatus, operation.status, "tool.status"),
    )
    if operation.timing is not None and not isinstance(operation.timing, Timing):
        raise TrajectoryValidationError("tool.timing must be Timing or null")
    if operation.failure is not None and not isinstance(operation.failure, TrajectoryFailure):
        raise TrajectoryValidationError("tool.failure must be TrajectoryFailure or null")
    if operation.retry_attempt is not None and operation.retry_of_record_id is None:
        raise TrajectoryValidationError("tool retry attempt requires a retry link")
    if operation.retry_attempt is not None and (
        type(operation.retry_attempt) is not int or operation.retry_attempt <= 0
    ):
        raise TrajectoryValidationError("tool.retry_attempt must be a positive integer")
    object.__setattr__(operation, "call_details", bound_detail_fields(operation.call_details))
    object.__setattr__(operation, "result_details", bound_detail_fields(operation.result_details))


def _validate_record_ids(operation: TrajectoryToolOperation) -> None:
    for name in ("call_record_ids", "result_record_ids", "child_call_ids"):
        value = getattr(operation, name)
        if not isinstance(value, tuple):
            raise TrajectoryValidationError(f"tool.{name} must be a tuple")
        bounded = tuple(
            bounded_text(
                item,
                max_bytes=TRAJECTORY_IDENTIFIER_MAX_BYTES,
                label=f"tool.{name}[]",
                nonempty=True,
            )
            for item in value
        )
        if len(bounded) > TRAJECTORY_TOOL_RECORD_LIMIT:
            raise TrajectoryValidationError(
                f"tool.{name} exceeds {TRAJECTORY_TOOL_RECORD_LIMIT} values"
            )
        if len(set(bounded)) != len(bounded):
            raise TrajectoryValidationError(f"tool.{name} must not repeat a value")
        object.__setattr__(operation, name, bounded)


def _validate_counts(operation: TrajectoryToolOperation) -> None:
    for name in ("call_count", "result_count"):
        value = getattr(operation, name)
        if type(value) is not int or value < 0:
            raise TrajectoryValidationError(f"tool.{name} must be a non-negative integer")
    if operation.call_count < len(operation.call_record_ids) or operation.result_count < len(
        operation.result_record_ids
    ):
        raise TrajectoryValidationError("tool record counts must cover retained record ids")
    if type(operation.records_truncated) is not bool:
        raise TrajectoryValidationError("tool.records_truncated must be a boolean")
    for count, record_ids in (
        (operation.call_count, operation.call_record_ids),
        (operation.result_count, operation.result_record_ids),
    ):
        if count == 0 and record_ids:
            raise TrajectoryValidationError("zero tool record counts must not retain record ids")
        if count > 0 and not record_ids:
            raise TrajectoryValidationError("positive tool record counts must retain record ids")
    truncated = operation.call_count > len(
        operation.call_record_ids
    ) or operation.result_count > len(operation.result_record_ids)
    if operation.records_truncated != truncated:
        raise TrajectoryValidationError("tool.records_truncated must match retained record ids")


__all__ = ["TrajectoryToolIdentity", "TrajectoryToolOperation", "tool_operations_for_records"]
