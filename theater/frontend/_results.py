"""Small typed result adapters shared by the public domain facades."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from theater.frontend.dto import EventCursor, EventTransaction, Job, Operation, Response
from theater.frontend.dto._wire import JSONValue, extras, freeze_json, object_value


@dataclass(frozen=True, slots=True)
class FrontendResult[T]:
    """A decoded success value plus its forward-compatible response envelope."""

    value: T
    response: Response

    @property
    def request_id(self) -> int:
        return self.response.request_id

    @property
    def extra(self) -> Mapping[str, JSONValue]:
        return self.response.extra


@dataclass(frozen=True, slots=True)
class Page[T]:
    """A bounded page retaining its opaque cursor and additive fields."""

    items: tuple[T, ...]
    next_cursor: str | None = None
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True, slots=True)
class AcceptedOperation:
    """The durable handle returned when an operation is accepted."""

    operation_id: str
    state: str
    participant_id: str | None = None
    job_handle: str | None = None
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True, slots=True)
class OperationAwaitResult:
    """An operation observation returned by a bounded wait."""

    operation: Operation
    timed_out: bool
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True, slots=True)
class JobsAwaitResult:
    """A job observation set returned by a bounded wait."""

    jobs: tuple[Job, ...]
    timed_out: bool
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True, slots=True)
class StateFollowResult:
    """Whole journal transactions and their durable ending cursor."""

    transactions: tuple[EventTransaction, ...]
    cursor: EventCursor
    timed_out: bool
    extra: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))


def decode_page[T](value: object, decoder: Callable[[object], T]) -> Page[T]:
    """Decode one frozen page while leaving additive page fields available."""
    data = object_value(value, "page result")
    raw_items = data.get("items")
    if not isinstance(raw_items, (list, tuple)):
        raise TypeError("page result.items must be an array")
    cursor = data.get("next_cursor")
    if cursor is not None and not isinstance(cursor, str):
        raise TypeError("page result.next_cursor must be a string or null")
    return Page(
        items=tuple(decoder(item) for item in raw_items),
        next_cursor=cursor,
        extra=extras(data, {"items", "next_cursor"}),
    )


def decode_accepted_operation(value: object) -> AcceptedOperation:
    """Decode the shared durable-operation acceptance result."""
    data = object_value(value, "accepted operation")
    operation_id = _identifier(data.get("operation_id"), "accepted operation.operation_id")
    state = _identifier(data.get("state"), "accepted operation.state")
    participant_id = _optional_string(
        data.get("participant_id"), "accepted operation.participant_id"
    )
    job_handle = _optional_string(data.get("job_handle"), "accepted operation.job_handle")
    return AcceptedOperation(
        operation_id=operation_id,
        state=state,
        participant_id=participant_id,
        job_handle=job_handle,
        extra=extras(data, {"operation_id", "state", "participant_id", "job_handle"}),
    )


def decode_operation_await(value: object) -> OperationAwaitResult:
    """Decode an operation wait result without inventing terminal state."""
    data = object_value(value, "operation await result")
    timed_out = data.get("timed_out")
    if type(timed_out) is not bool:
        raise TypeError("operation await result.timed_out must be a boolean")
    return OperationAwaitResult(
        operation=Operation.from_wire(data.get("operation")),
        timed_out=timed_out,
        extra=extras(data, {"operation", "timed_out"}),
    )


def decode_jobs_await(value: object) -> JobsAwaitResult:
    """Decode a job wait result while retaining unknown job values."""
    data = object_value(value, "jobs await result")
    raw_jobs = data.get("jobs")
    timed_out = data.get("timed_out")
    if not isinstance(raw_jobs, (list, tuple)):
        raise TypeError("jobs await result.jobs must be an array")
    if type(timed_out) is not bool:
        raise TypeError("jobs await result.timed_out must be a boolean")
    return JobsAwaitResult(
        jobs=tuple(Job.from_wire(item) for item in raw_jobs),
        timed_out=timed_out,
        extra=extras(data, {"jobs", "timed_out"}),
    )


def decode_state_follow(value: object) -> StateFollowResult:
    """Decode whole event transactions and retain forward-compatible values."""
    data = object_value(value, "state follow result")
    raw_transactions = data.get("transactions")
    timed_out = data.get("timed_out")
    if not isinstance(raw_transactions, (list, tuple)):
        raise TypeError("state follow result.transactions must be an array")
    if type(timed_out) is not bool:
        raise TypeError("state follow result.timed_out must be a boolean")
    return StateFollowResult(
        transactions=tuple(EventTransaction.from_wire(item) for item in raw_transactions),
        cursor=EventCursor.from_wire(data.get("cursor")),
        timed_out=timed_out,
        extra=extras(data, {"transactions", "cursor", "timed_out"}),
    )


def freeze_object(value: object, label: str = "result") -> Mapping[str, JSONValue]:
    """Return a recursively immutable arbitrary object result."""
    frozen = freeze_json(object_value(value, label), label)
    assert isinstance(frozen, Mapping)
    return frozen


def result_of[T](response: Response, decoder: Callable[[object], T]) -> FrontendResult[T]:
    """Preserve response extras while adapting a successful result to a public value."""
    return FrontendResult(value=decoder(response.result), response=response)


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"{label} must be a non-empty string")
    return value


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, label)


__all__ = [
    "AcceptedOperation",
    "FrontendResult",
    "JobsAwaitResult",
    "OperationAwaitResult",
    "Page",
    "StateFollowResult",
    "decode_accepted_operation",
    "decode_jobs_await",
    "decode_operation_await",
    "decode_page",
    "decode_state_follow",
    "freeze_object",
    "result_of",
]
