"""Durable operation acceptance, state transitions, waiting, and execution."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, replace
from typing import Protocol

from jsonschema.exceptions import ValidationError
from sqlalchemy.exc import IntegrityError

from theater.daemon.operations.digest import request_digest
from theater.daemon.operations.errors import (
    IdempotencyConflict,
    InvalidOperationTransition,
    OperationNotFound,
)
from theater.daemon.operations.notifications import OperationNotifier
from theater.daemon.operations.projection import operation_event_payload, operation_to_wire
from theater.daemon.persistence.repositories._json import encode_json
from theater.daemon.persistence.repositories.journal import JournalRepository
from theater.daemon.persistence.repositories.operations import OperationRepository
from theater.daemon.persistence.transactions import WriteUnit
from theater.frontend.capabilities import METHOD_CATALOG, MethodClass
from theater.frontend.schemas import validate_public_request, validator_for
from theater.models import (
    IdempotencyRecord,
    JournalEventRecord,
    PublicOperationRecord,
    PublicOperationState,
    new_id,
    now,
)

logger = logging.getLogger("theater.daemon.operations")

DEFAULT_WAIT_SECONDS = 25.0
MAX_WAIT_SECONDS = 300.0
IDEMPOTENCY_RETENTION_SECONDS = 7 * 24 * 60 * 60
UNSETTLED_STATES = frozenset(
    {
        PublicOperationState.ACCEPTED.value,
        PublicOperationState.RUNNING.value,
        PublicOperationState.UNCERTAIN.value,
    }
)
TERMINAL_STATES = frozenset(
    {PublicOperationState.SUCCEEDED.value, PublicOperationState.FAILED.value}
)


class OperationStore(Protocol):
    operations: OperationRepository
    journal: JournalRepository

    def write_unit(self) -> WriteUnit: ...


@dataclass(frozen=True, slots=True)
class PreparedOperation:
    record: PublicOperationRecord
    response: Mapping[str, object]
    events: tuple[JournalEventRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class OperationAcceptance:
    record: PublicOperationRecord
    response: Mapping[str, object]
    replayed: bool


@dataclass(frozen=True, slots=True)
class IdempotentResult:
    value: object
    replayed: bool


@dataclass(frozen=True, slots=True)
class DispatchIntent:
    phase: str
    provider_id: str | None = None
    provider_generation: int | None = None
    terminal_id: str | None = None
    terminal_incarnation: str | None = None
    occupant_evidence: Mapping[str, object] | None = None
    process_facts: Mapping[str, object] | None = None
    backend_generation: int | None = None
    native_session_id: str | None = None
    native_turn_id: str | None = None
    composite_termination: bool = False


@dataclass(frozen=True, slots=True)
class OperationOutcome:
    state: str
    phase: str
    result: object | None = None
    error: Mapping[str, object] | None = None

    @classmethod
    def succeeded(cls, *, phase: str, result: object | None = None) -> OperationOutcome:
        return cls(PublicOperationState.SUCCEEDED.value, phase, result=result)

    @classmethod
    def failed(cls, *, phase: str, error: Mapping[str, object]) -> OperationOutcome:
        return cls(PublicOperationState.FAILED.value, phase, error=error)

    @classmethod
    def uncertain(cls, *, phase: str, error: Mapping[str, object]) -> OperationOutcome:
        return cls(PublicOperationState.UNCERTAIN.value, phase, error=error)


@dataclass(frozen=True, slots=True)
class ReconcileEvidence:
    observed_updated_at: float
    outcome: OperationOutcome


OperationBuilder = Callable[[str, WriteUnit], PreparedOperation]
IdempotentAction = Callable[[WriteUnit], object]
OperationSideEffect = Callable[[], Awaitable[OperationOutcome]]
EvidenceReconciler = Callable[[PublicOperationRecord], Awaitable[ReconcileEvidence | None]]


class OperationService:
    """The daemon-owned operation service; every database mutation is synchronous."""

    def __init__(
        self,
        store: OperationStore,
        *,
        clock: Callable[[], float] = now,
        id_factory: Callable[[], str] = new_id,
        notifier: OperationNotifier | None = None,
        reconciler: EvidenceReconciler | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._id_factory = id_factory
        self._notifier = notifier or OperationNotifier()
        self._reconciler = reconciler
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def accept_operation(
        self,
        *,
        client_id: str,
        idempotency_key: str,
        method: str,
        params: Mapping[str, object],
        prepare: OperationBuilder,
    ) -> OperationAcceptance:
        """Claim a key and create its accepted operation in one write unit."""
        self._validate_idempotent_request(method, params, idempotency_key, MethodClass.OPERATION)
        digest = request_digest(method, params)
        timestamp = self._clock()
        existing = self._active_idempotency(client_id, idempotency_key, timestamp)
        if existing is not None:
            return self._operation_replay(existing, method, digest)

        operation_id = self._id_factory()
        try:
            with self._store.write_unit() as unit:
                current = self._store.operations.get_idempotency(
                    client_id, idempotency_key, connection=unit.connection
                )
                if current is not None and self._idempotency_is_active(
                    current, timestamp, connection=unit.connection
                ):
                    return self._operation_replay(
                        current, method, digest, connection=unit.connection
                    )
                if current is not None:
                    self._store.operations.delete_idempotency(
                        client_id, idempotency_key, connection=unit.connection
                    )
                self._store.operations.claim_idempotency(
                    IdempotencyRecord(
                        client_id=client_id,
                        key=idempotency_key,
                        method=method,
                        payload_digest=digest,
                        operation_id=operation_id,
                        created_at=timestamp,
                    ),
                    connection=unit.connection,
                )
                prepared = prepare(operation_id, unit)
                self._validate_prepared(method, client_id, operation_id, prepared)
                self._store.operations.create(prepared.record, connection=unit.connection)
                if not self._store.operations.complete_idempotency(
                    client_id,
                    idempotency_key,
                    response=dict(prepared.response),
                    settled_at=None,
                    retain_until=None,
                    connection=unit.connection,
                ):
                    raise RuntimeError(
                        "accepted idempotency claim disappeared inside its write unit"
                    )
                self._append_event(unit, prepared.record, preceding=prepared.events)
                unit.after_commit(lambda: self._notifier.notify(operation_id))
        except IntegrityError:
            winner = self._active_idempotency(client_id, idempotency_key, self._clock())
            if winner is None:
                raise
            return self._operation_replay(winner, method, digest)
        return OperationAcceptance(prepared.record, dict(prepared.response), replayed=False)

    def execute_idempotent(
        self,
        *,
        client_id: str,
        idempotency_key: str,
        method: str,
        params: Mapping[str, object],
        action: IdempotentAction,
    ) -> IdempotentResult:
        """Run one synchronous write action after atomically winning its key."""
        self._validate_idempotent_request(method, params, idempotency_key, MethodClass.WRITE)
        digest = request_digest(method, params)
        timestamp = self._clock()
        existing = self._active_idempotency(client_id, idempotency_key, timestamp)
        if existing is not None:
            return IdempotentResult(self._replay_value(existing, method, digest), replayed=True)
        try:
            with self._store.write_unit() as unit:
                current = self._store.operations.get_idempotency(
                    client_id, idempotency_key, connection=unit.connection
                )
                if current is not None and self._idempotency_is_active(
                    current, timestamp, connection=unit.connection
                ):
                    return IdempotentResult(
                        self._replay_value(current, method, digest), replayed=True
                    )
                if current is not None:
                    self._store.operations.delete_idempotency(
                        client_id, idempotency_key, connection=unit.connection
                    )
                self._store.operations.claim_idempotency(
                    IdempotencyRecord(
                        client_id=client_id,
                        key=idempotency_key,
                        method=method,
                        payload_digest=digest,
                        created_at=timestamp,
                    ),
                    connection=unit.connection,
                )
                result = action(unit)
                validator_for(METHOD_CATALOG[method].result_schema_id).validate(result)
                settled_at = self._clock()
                if not self._store.operations.complete_idempotency(
                    client_id,
                    idempotency_key,
                    response=result,
                    settled_at=settled_at,
                    retain_until=settled_at + IDEMPOTENCY_RETENTION_SECONDS,
                    connection=unit.connection,
                ):
                    raise RuntimeError(
                        "completed idempotency claim disappeared inside its write unit"
                    )
        except IntegrityError:
            winner = self._active_idempotency(client_id, idempotency_key, self._clock())
            if winner is None:
                raise
            return IdempotentResult(self._replay_value(winner, method, digest), replayed=True)
        return IdempotentResult(result, replayed=False)

    def replay_idempotent_write(
        self,
        *,
        client_id: str,
        idempotency_key: str,
        method: str,
        params: Mapping[str, object],
    ) -> IdempotentResult | None:
        """Return a live cached write result before mutable admission work."""
        self._validate_idempotent_request(method, params, idempotency_key, MethodClass.WRITE)
        existing = self._active_idempotency(client_id, idempotency_key, self._clock())
        if existing is None:
            return None
        return IdempotentResult(
            self._replay_value(existing, method, request_digest(method, params)),
            replayed=True,
        )

    def get(self, operation_id: str) -> PublicOperationRecord:
        record = self._store.operations.get(operation_id)
        if record is None:
            raise OperationNotFound(operation_id)
        return record

    def list(
        self,
        *,
        cursor: str | None = None,
        limit: int = 200,
        unsettled_only: bool = False,
        target_id: str | None = None,
    ) -> tuple[tuple[PublicOperationRecord, ...], str | None]:
        if not 1 <= limit <= 500:
            raise ValueError("operation page limit must be between 1 and 500")
        try:
            return self._store.operations.list_page(
                cursor=cursor,
                limit=limit,
                unsettled_only=unsettled_only,
                target_id=target_id,
            )
        except KeyError as exc:
            raise ValueError(f"unknown operation cursor {cursor!r}") from exc

    async def wait(
        self, operation_id: str, *, wait_seconds: float = DEFAULT_WAIT_SECONDS
    ) -> tuple[PublicOperationRecord, bool]:
        """Wait for settlement without holding storage or trusting a wakeup."""
        if not 0 <= wait_seconds <= MAX_WAIT_SECONDS:
            raise ValueError(f"wait_seconds must be between 0 and {MAX_WAIT_SECONDS:g}")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_seconds
        while True:
            before = self.get(operation_id)
            if before.state in TERMINAL_STATES:
                return before, False
            subscription = self._notifier.subscribe(operation_id)
            try:
                current = self.get(operation_id)
                if current.state in TERMINAL_STATES:
                    return current, False
                if current.updated_at != before.updated_at or current.state != before.state:
                    continue
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return self.get(operation_id), True
                try:
                    await asyncio.wait_for(subscription.wait(), timeout=remaining)
                except TimeoutError:
                    current = self.get(operation_id)
                    return current, current.state not in TERMINAL_STATES
            finally:
                subscription.close()

    async def reconcile(self, operation_id: str) -> PublicOperationRecord:
        """Settle uncertainty only when the configured evidence source proves it."""
        current = self.get(operation_id)
        if current.state != PublicOperationState.UNCERTAIN.value or self._reconciler is None:
            return current
        evidence = await self._reconciler(current)
        if evidence is None:
            return self.get(operation_id)
        if evidence.outcome.state not in TERMINAL_STATES:
            raise ValueError("reconciliation evidence must prove success or failure")
        return self._apply_outcome(
            operation_id,
            evidence.outcome,
            expected_updated_at=evidence.observed_updated_at,
        )

    def mark_running(self, operation_id: str, *, phase: str) -> PublicOperationRecord:
        return self._transition(
            operation_id,
            state=PublicOperationState.RUNNING.value,
            phase=phase,
            allowed={PublicOperationState.ACCEPTED.value, PublicOperationState.RUNNING.value},
        )

    def mark_dispatch_intent(
        self, operation_id: str, intent: DispatchIntent
    ) -> PublicOperationRecord:
        self._validate_dispatch_intent(intent)
        return self._transition(
            operation_id,
            state=PublicOperationState.RUNNING.value,
            phase=intent.phase,
            allowed={PublicOperationState.ACCEPTED.value},
            dispatch=intent,
        )

    def mark_provider_dispatch_target(
        self,
        operation_id: str,
        *,
        provider_id: str,
        provider_generation: int,
        phase: str,
    ) -> PublicOperationRecord:
        intent = DispatchIntent(
            phase=phase,
            provider_id=provider_id,
            provider_generation=provider_generation,
        )
        self._validate_dispatch_intent(intent)
        return self._transition(
            operation_id,
            state=None,
            phase=phase,
            allowed={PublicOperationState.RUNNING.value},
            dispatch=intent,
        )

    def mark_uncertain(
        self,
        operation_id: str,
        *,
        phase: str,
        error: Mapping[str, object],
    ) -> PublicOperationRecord:
        return self._transition(
            operation_id,
            state=PublicOperationState.UNCERTAIN.value,
            phase=phase,
            allowed={PublicOperationState.RUNNING.value, PublicOperationState.UNCERTAIN.value},
            error=error,
        )

    def succeed(
        self, operation_id: str, *, phase: str, result: object | None = None
    ) -> PublicOperationRecord:
        return self._transition(
            operation_id,
            state=PublicOperationState.SUCCEEDED.value,
            phase=phase,
            allowed={PublicOperationState.RUNNING.value, PublicOperationState.UNCERTAIN.value},
            result=result,
        )

    def fail(
        self,
        operation_id: str,
        *,
        phase: str,
        error: Mapping[str, object],
    ) -> PublicOperationRecord:
        return self._transition(
            operation_id,
            state=PublicOperationState.FAILED.value,
            phase=phase,
            allowed=UNSETTLED_STATES,
            error=error,
        )

    def link(
        self,
        operation_id: str,
        *,
        phase: str,
        control_operation_id: str | None = None,
        job_handle: str | None = None,
    ) -> PublicOperationRecord:
        return self._transition(
            operation_id,
            state=None,
            phase=phase,
            allowed={PublicOperationState.ACCEPTED.value, PublicOperationState.RUNNING.value},
            control_operation_id=control_operation_id,
            job_handle=job_handle,
        )

    def submit(
        self,
        *,
        client_id: str,
        idempotency_key: str,
        method: str,
        params: Mapping[str, object],
        prepare: OperationBuilder,
        dispatch: DispatchIntent,
        side_effect: OperationSideEffect,
    ) -> OperationAcceptance:
        """Accept durably, then schedule work owned independently of the request."""
        self._validate_dispatch_intent(dispatch)
        acceptance = self.accept_operation(
            client_id=client_id,
            idempotency_key=idempotency_key,
            method=method,
            params=params,
            prepare=prepare,
        )
        if not acceptance.replayed:
            self.start(acceptance.record.operation_id, dispatch=dispatch, side_effect=side_effect)
        return acceptance

    def start(
        self,
        operation_id: str,
        *,
        dispatch: DispatchIntent,
        side_effect: OperationSideEffect,
    ) -> asyncio.Task[None]:
        existing = self._tasks.get(operation_id)
        if existing is not None and not existing.done():
            return existing
        current = self.get(operation_id)
        if current.state != PublicOperationState.ACCEPTED.value:
            raise InvalidOperationTransition(
                operation_id, current.state, PublicOperationState.RUNNING.value
            )
        task = asyncio.create_task(
            self._run_dispatched(operation_id, dispatch, side_effect),
            name=f"public-operation-{operation_id}",
        )
        self._tasks[operation_id] = task
        task.add_done_callback(lambda completed: self._task_done(operation_id, completed))
        return task

    async def aclose(self) -> None:
        tasks = tuple(task for task in self._tasks.values() if not task.done())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def close(self) -> None:
        await self.aclose()

    @property
    def owned_tasks(self) -> tuple[asyncio.Task[None], ...]:
        return tuple(task for task in self._tasks.values() if not task.done())

    async def _run_dispatched(
        self,
        operation_id: str,
        dispatch: DispatchIntent,
        side_effect: OperationSideEffect,
    ) -> None:
        self.mark_dispatch_intent(operation_id, dispatch)
        try:
            outcome = await side_effect()
        except asyncio.CancelledError:
            self._mark_uncertain_after_loss(operation_id, "dispatch_cancelled")
            raise
        except Exception:
            logger.exception("public operation side effect lost a definitive outcome")
            self._mark_uncertain_after_loss(operation_id, "dispatch_error")
            return
        try:
            self._apply_outcome(operation_id, outcome)
        except Exception:
            logger.exception("public operation outcome could not be persisted")
            self._mark_uncertain_after_loss(operation_id, "outcome_persistence_error")

    def _mark_uncertain_after_loss(self, operation_id: str, phase: str) -> None:
        try:
            self.mark_uncertain(
                operation_id,
                phase=phase,
                error={
                    "code": "internal",
                    "message": "the side effect may have executed; reconcile from durable evidence",
                    "details": {"reason": phase},
                },
            )
        except Exception:
            logger.exception("could not persist uncertain operation state")

    def _apply_outcome(
        self,
        operation_id: str,
        outcome: OperationOutcome,
        *,
        expected_updated_at: float | None = None,
    ) -> PublicOperationRecord:
        if outcome.state == PublicOperationState.SUCCEEDED.value:
            return self._transition(
                operation_id,
                state=outcome.state,
                phase=outcome.phase,
                allowed={PublicOperationState.RUNNING.value, PublicOperationState.UNCERTAIN.value},
                result=outcome.result,
                expected_updated_at=expected_updated_at,
            )
        if outcome.state in {
            PublicOperationState.FAILED.value,
            PublicOperationState.UNCERTAIN.value,
        }:
            if outcome.error is None:
                raise ValueError(f"{outcome.state} operation outcomes require an error")
            allowed = (
                {PublicOperationState.RUNNING.value, PublicOperationState.UNCERTAIN.value}
                if outcome.state == PublicOperationState.FAILED.value
                else {PublicOperationState.RUNNING.value}
            )
            return self._transition(
                operation_id,
                state=outcome.state,
                phase=outcome.phase,
                allowed=allowed,
                error=outcome.error,
                expected_updated_at=expected_updated_at,
            )
        raise ValueError("side effects may finish only as succeeded, failed, or uncertain")

    def _transition(
        self,
        operation_id: str,
        *,
        state: str | None,
        phase: str,
        allowed: Collection[str],
        result: object | None = None,
        error: Mapping[str, object] | None = None,
        dispatch: DispatchIntent | None = None,
        control_operation_id: str | None = None,
        job_handle: str | None = None,
        expected_updated_at: float | None = None,
    ) -> PublicOperationRecord:
        if not phase:
            raise ValueError("operation phase must be non-empty")
        with self._store.write_unit() as unit:
            current = self._store.operations.get(operation_id, connection=unit.connection)
            if current is None:
                raise OperationNotFound(operation_id)
            if expected_updated_at is not None and current.updated_at != expected_updated_at:
                return current
            requested = current.state if state is None else state
            if current.state not in allowed:
                if current.state == requested and current.state in TERMINAL_STATES:
                    return current
                raise InvalidOperationTransition(operation_id, current.state, requested)
            if control_operation_id is not None and current.control_operation_id not in {
                None,
                control_operation_id,
            }:
                raise ValueError("an operation cannot be relinked to another control operation")
            if job_handle is not None and current.job_handle not in {None, job_handle}:
                raise ValueError("an operation cannot be relinked to another job")
            timestamp = _next_timestamp(self._clock(), current.updated_at)
            error_value = _validated_error(error) if error is not None else None
            terminal = requested in TERMINAL_STATES
            updated = replace(
                current,
                state=requested,
                phase=phase,
                updated_at=timestamp,
                settled_at=timestamp if terminal else None,
                result=(
                    result
                    if state is not None and requested == PublicOperationState.SUCCEEDED.value
                    else current.result
                    if state is None
                    else None
                ),
                error=error_value if state is not None else current.error,
                error_code=(
                    str(error_value["code"])
                    if state is not None and error_value is not None
                    else current.error_code
                    if state is None
                    else None
                ),
                dispatch_provider_id=(
                    dispatch.provider_id if dispatch is not None else current.dispatch_provider_id
                ),
                dispatch_provider_generation=(
                    dispatch.provider_generation
                    if dispatch is not None
                    else current.dispatch_provider_generation
                ),
                dispatch_terminal_id=(
                    dispatch.terminal_id if dispatch is not None else current.dispatch_terminal_id
                ),
                dispatch_terminal_incarnation=(
                    dispatch.terminal_incarnation
                    if dispatch is not None
                    else current.dispatch_terminal_incarnation
                ),
                dispatch_terminal_occupant_evidence=(
                    dict(dispatch.occupant_evidence)
                    if dispatch is not None and dispatch.occupant_evidence is not None
                    else current.dispatch_terminal_occupant_evidence
                ),
                dispatch_terminal_process_facts=(
                    dict(dispatch.process_facts)
                    if dispatch is not None and dispatch.process_facts is not None
                    else current.dispatch_terminal_process_facts
                ),
                dispatch_backend_generation=(
                    dispatch.backend_generation
                    if dispatch is not None
                    else current.dispatch_backend_generation
                ),
                dispatch_native_session_id=(
                    dispatch.native_session_id
                    if dispatch is not None
                    else current.dispatch_native_session_id
                ),
                dispatch_native_turn_id=(
                    dispatch.native_turn_id
                    if dispatch is not None
                    else current.dispatch_native_turn_id
                ),
                control_operation_id=(
                    control_operation_id
                    if control_operation_id is not None
                    else current.control_operation_id
                ),
                job_handle=job_handle if job_handle is not None else current.job_handle,
            )
            _validate_operation(updated)
            if not self._store.operations.replace(
                updated,
                expected_state=current.state,
                expected_updated_at=current.updated_at,
                connection=unit.connection,
            ):
                raise RuntimeError("operation changed during its synchronous write unit")
            if terminal:
                self._store.operations.settle_idempotency_for_operation(
                    operation_id,
                    settled_at=timestamp,
                    retain_until=timestamp + IDEMPOTENCY_RETENTION_SECONDS,
                    connection=unit.connection,
                )
            self._append_event(unit, updated)
            unit.after_commit(lambda: self._notifier.notify(operation_id))
        return updated

    def _append_event(
        self,
        unit: WriteUnit,
        record: PublicOperationRecord,
        *,
        preceding: tuple[JournalEventRecord, ...] = (),
    ) -> None:
        revision = (
            self._store.journal.current_sequence(connection=unit.connection) + len(preceding) + 1
        )
        self._store.journal.append_group(
            unit,
            [
                *preceding,
                JournalEventRecord(
                    kind="operation.updated",
                    entity_id=record.operation_id,
                    entity_revision=revision,
                    payload=operation_event_payload(record),
                    recorded_at=record.updated_at,
                ),
            ],
        )

    def _active_idempotency(
        self, client_id: str, key: str, timestamp: float
    ) -> IdempotencyRecord | None:
        record = self._store.operations.get_idempotency(client_id, key)
        if record is None:
            return None
        return record if self._idempotency_is_active(record, timestamp) else None

    def _idempotency_is_active(
        self,
        record: IdempotencyRecord,
        timestamp: float,
        *,
        connection=None,
    ) -> bool:
        if record.retain_until is None or record.retain_until > timestamp:
            return True
        if record.operation_id is None:
            return False
        operation = self._store.operations.get(record.operation_id, connection=connection)
        return operation is not None and operation.state in UNSETTLED_STATES

    def _operation_replay(
        self,
        record: IdempotencyRecord,
        method: str,
        digest: str,
        *,
        connection=None,
    ) -> OperationAcceptance:
        response = self._replay_value(record, method, digest)
        if not isinstance(response, Mapping) or record.operation_id is None:
            raise RuntimeError("stored operation idempotency result is incomplete")
        operation = self._store.operations.get(record.operation_id, connection=connection)
        if operation is None:
            raise RuntimeError("stored idempotency result references a missing operation")
        return OperationAcceptance(operation, dict(response), replayed=True)

    @staticmethod
    def _replay_value(record: IdempotencyRecord, method: str, digest: str) -> object:
        if record.method != method or record.payload_digest != digest:
            raise IdempotencyConflict(
                client_id=record.client_id,
                key=record.key,
                requested_method=method,
                original_method=record.method,
                operation_id=record.operation_id,
            )
        if record.response is None:
            raise RuntimeError("stored idempotency claim has no completed response")
        return record.response

    @staticmethod
    def _validate_idempotent_request(
        method: str,
        params: Mapping[str, object],
        key: str,
        expected_class: MethodClass,
    ) -> None:
        spec = METHOD_CATALOG.get(method)
        if spec is None or spec.method_class is not expected_class:
            raise ValueError(f"{method!r} is not a public {expected_class.value} method")
        validate_public_request(
            {"id": 1, "method": method, "params": dict(params), "idempotency_key": key}
        )

    @staticmethod
    def _validate_prepared(
        method: str,
        client_id: str,
        operation_id: str,
        prepared: PreparedOperation,
    ) -> None:
        record = prepared.record
        if record.operation_id != operation_id or record.actor_client_id != client_id:
            raise ValueError("prepared operation identity does not match its acceptance claim")
        if record.state != PublicOperationState.ACCEPTED.value or record.settled_at is not None:
            raise ValueError("new public operations must begin accepted and unsettled")
        if not record.kind or not record.phase:
            raise ValueError("new public operations require a kind and detailed phase")
        if record.result is not None or record.error is not None or record.error_code is not None:
            raise ValueError("new public operations cannot begin with a result or error")
        dispatch_values = (
            record.dispatch_provider_id,
            record.dispatch_provider_generation,
            record.dispatch_terminal_id,
            record.dispatch_terminal_incarnation,
            record.dispatch_terminal_occupant_evidence,
            record.dispatch_terminal_process_facts,
            record.dispatch_backend_generation,
            record.dispatch_native_session_id,
            record.dispatch_native_turn_id,
        )
        if any(value is not None for value in dispatch_values):
            raise ValueError("new public operations cannot begin with a dispatch identity")
        if not math.isfinite(record.created_at) or not math.isfinite(record.updated_at):
            raise ValueError("operation timestamps must be finite")
        _validate_operation(record)
        validator_for(METHOD_CATALOG[method].result_schema_id).validate(prepared.response)
        if prepared.response.get("operation_id") != operation_id:
            raise ValueError("accepted response must contain the claimed operation ID")

    @staticmethod
    def _validate_dispatch_intent(intent: DispatchIntent) -> None:
        if not intent.phase:
            raise ValueError("dispatch intent phase must be non-empty")
        terminal = (
            intent.terminal_id,
            intent.terminal_incarnation,
            intent.occupant_evidence,
        )
        has_provider = _provider_dispatch_present(intent)
        has_terminal = any(value is not None for value in (*terminal, intent.process_facts))
        if has_terminal and (not has_provider or any(value is None for value in terminal)):
            raise ValueError(
                "terminal dispatch requires provider, generation, ID, incarnation, and occupant"
            )
        native = (intent.backend_generation, intent.native_session_id, intent.native_turn_id)
        has_native = any(value is not None for value in native)
        if has_native and (intent.backend_generation is None or intent.native_session_id is None):
            raise ValueError("native dispatch requires a backend generation and session ID")
        _validate_dispatch_composition(intent, has_provider, has_terminal, has_native)
        for evidence, label in (
            (intent.occupant_evidence, "terminal occupant evidence"),
            (intent.process_facts, "terminal process facts"),
        ):
            if evidence is not None and not isinstance(evidence, Mapping):
                raise TypeError(f"{label} must be an object")
        if has_terminal:
            terminal_identity = {
                "provider_id": intent.provider_id,
                "provider_generation": intent.provider_generation,
                "terminal_id": intent.terminal_id,
                "terminal_incarnation": intent.terminal_incarnation,
                "occupant": dict(intent.occupant_evidence or {}),
                "process": None if intent.process_facts is None else dict(intent.process_facts),
            }
            try:
                validator_for(
                    "https://theater.dev/schemas/frontend/1.0/common.json#/$defs/terminalIdentity"
                ).validate(terminal_identity)
                encode_json(terminal_identity)
            except (TypeError, ValueError, ValidationError) as exc:
                raise ValueError(
                    f"terminal dispatch identity does not match the public contract: {exc}"
                ) from exc
        for generation in (intent.provider_generation, intent.backend_generation):
            if generation is not None and (type(generation) is not int or generation < 0):
                raise ValueError("dispatch generations must be non-negative integers")
        for identity_value in (
            intent.provider_id,
            intent.terminal_id,
            intent.terminal_incarnation,
            intent.native_session_id,
            intent.native_turn_id,
        ):
            if identity_value is not None and (
                not isinstance(identity_value, str) or not 1 <= len(identity_value) <= 512
            ):
                raise ValueError("dispatch identity strings must contain 1 to 512 characters")

    def _task_done(self, operation_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(operation_id) is task:
            self._tasks.pop(operation_id, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error("detached public operation task failed", exc_info=task.exception())


def _provider_dispatch_present(intent: DispatchIntent) -> bool:
    values = (intent.provider_id, intent.provider_generation)
    if any(value is not None for value in values) and any(value is None for value in values):
        raise ValueError("provider dispatch requires both provider and generation")
    return all(value is not None for value in values)


def _validate_dispatch_composition(
    intent: DispatchIntent,
    has_provider: bool,
    has_terminal: bool,
    has_native: bool,
) -> None:
    if intent.composite_termination and not (has_terminal and has_native):
        raise ValueError(
            "composite termination dispatch requires exact terminal and native identities"
        )
    if has_provider and has_native and not intent.composite_termination:
        raise ValueError("one operation dispatch cannot target terminal and native routes")


def _validated_error(error: Mapping[str, object]) -> dict[str, object]:
    value = dict(error)
    if not isinstance(value.get("code"), str) or not value["code"]:
        raise ValueError("operation error requires a non-empty code")
    if not isinstance(value.get("message"), str):
        raise TypeError("operation error requires a message")
    try:
        validator_for("https://theater.dev/schemas/frontend/1.0/common.json#/$defs/error").validate(
            value
        )
    except ValidationError as exc:
        raise ValueError(f"operation error does not match the public contract: {exc}") from exc
    return value


def _validate_operation(record: PublicOperationRecord) -> None:
    try:
        validator_for(
            "https://theater.dev/schemas/frontend/1.0/common.json#/$defs/operation"
        ).validate(operation_to_wire(record))
    except ValidationError as exc:
        raise ValueError(f"operation does not match the public contract: {exc}") from exc


def _next_timestamp(candidate: float, previous: float) -> float:
    return candidate if candidate > previous else math.nextafter(previous, math.inf)


__all__ = [
    "DEFAULT_WAIT_SECONDS",
    "IDEMPOTENCY_RETENTION_SECONDS",
    "MAX_WAIT_SECONDS",
    "TERMINAL_STATES",
    "UNSETTLED_STATES",
    "DispatchIntent",
    "EvidenceReconciler",
    "IdempotentResult",
    "OperationAcceptance",
    "OperationBuilder",
    "OperationOutcome",
    "OperationService",
    "OperationSideEffect",
    "PreparedOperation",
    "ReconcileEvidence",
]
