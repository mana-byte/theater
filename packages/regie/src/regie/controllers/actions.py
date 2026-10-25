"""Durable public mutation tracking for Régie's user actions."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from time import monotonic
from typing import Literal
from uuid import uuid4

from regie.constants import REGIE_ACTION_HISTORY_LIMIT, REGIE_IDLE_ACTION_CLIENTS
from regie.controllers.spawn_trace import SpawnTrace
from theater.frontend import (
    AcceptedOperation,
    FrontendClient,
    FrontendClientError,
    FrontendResponseError,
    FrontendResult,
    FrontendTransportError,
    Operation,
    ResponseCorrelationError,
    ResponseValidationError,
)

logger = logging.getLogger(__name__)


class ActionState(StrEnum):
    PENDING = "pending"
    UNCERTAIN = "uncertain"
    REFUSED = "refused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(slots=True)
class ActionRecord:
    action: str
    target_id: str
    idempotency_key: str
    participant_id: str | None = None
    state: ActionState = ActionState.PENDING
    operation_id: str | None = None
    job_handle: str | None = None
    phase: str | None = None
    result: object = None
    error_code: str | None = None
    detail: str | None = None
    submitted_at: float = field(default_factory=monotonic)
    observed_at: float | None = None


type ClientFactory = Callable[[], FrontendClient]
type RequestFactory = Callable[[FrontendClient, str], Awaitable[FrontendResult[AcceptedOperation]]]
type ResultRequestFactory = Callable[[FrontendClient, str], Awaitable[FrontendResult[object]]]
type ActionIdentity = tuple[str, str]


@dataclass(frozen=True, slots=True)
class _OperationRequest:
    kind: Literal["operation"]
    send: RequestFactory


@dataclass(frozen=True, slots=True)
class _ResultRequest:
    kind: Literal["result"]
    send: ResultRequestFactory


@dataclass(slots=True)
class _ActiveAction:
    record: ActionRecord
    request: _OperationRequest | _ResultRequest | None = None
    client: FrontendClient | None = None
    wait: asyncio.Task[None] | None = None


class OperationController:
    """Retain one idempotency key per visible action and never replay automatically."""

    def __init__(
        self,
        client: FrontendClient,
        *,
        client_factory: ClientFactory | None = None,
        on_change: Callable[[ActionRecord], None] | None = None,
        history_limit: int = REGIE_ACTION_HISTORY_LIMIT,
    ) -> None:
        if history_limit < 0:
            raise ValueError("action history limit must be non-negative")
        self._client = client
        self._client_factory = client_factory
        self._on_change = on_change
        self._records: dict[ActionIdentity, ActionRecord] = {}
        self._active: dict[ActionIdentity, _ActiveAction] = {}
        self._settled: OrderedDict[ActionIdentity, ActionRecord] = OrderedDict()
        self._history_limit = history_limit
        self._owned_clients: dict[int, FrontendClient] = {}
        self._idle_clients: list[FrontendClient] = []
        self._client_locks: dict[int, asyncio.Lock] = {}
        self._spawn_targets: dict[tuple[str, str, str, str], str] = {}
        self._closed = False
        self.spawn_trace = SpawnTrace()

    @property
    def records(self) -> tuple[ActionRecord, ...]:
        return tuple(self._records.values())

    def record(self, action: str, target_id: str) -> ActionRecord | None:
        return self._records.get((action, target_id))

    def acknowledge(self, record: ActionRecord) -> None:
        """Bound settled history only after the UI has reconciled the action."""
        identity = (record.action, record.target_id)
        if self._records.get(identity) is not record or not self._is_terminal(record):
            return
        self._settled[identity] = record
        self._settled.move_to_end(identity)
        while len(self._settled) > self._history_limit:
            previous, retained = self._settled.popitem(last=False)
            if self._records.get(previous) is retained:
                self._records.pop(previous)

    def refuse_locally(self, action: str, target_id: str, reason: str) -> ActionRecord:
        """Represent a current public-capability refusal without sending a mutation."""
        identity = (action, target_id)
        existing = self.record(action, target_id)
        if existing is not None and existing.state in {ActionState.PENDING, ActionState.UNCERTAIN}:
            return existing
        record = ActionRecord(
            action,
            target_id,
            uuid4().hex,
            participant_id=target_id,
            state=ActionState.REFUSED,
            detail=reason,
        )
        self._records[identity] = record
        return record

    async def send(self, participant_id: str, prompt: str) -> ActionRecord:
        return await self._submit(
            "send",
            participant_id,
            lambda client, key: client.controls.send(participant_id, prompt, idempotency_key=key),
        )

    async def queue_followup(self, participant_id: str, prompt: str) -> ActionRecord:
        return await self._submit(
            "queue_followup",
            participant_id,
            lambda client, key: client.controls.queue_followup(
                participant_id, prompt, idempotency_key=key
            ),
        )

    async def interrupt(self, participant_id: str) -> ActionRecord:
        return await self._submit(
            "interrupt",
            participant_id,
            lambda client, key: client.controls.interrupt(participant_id, idempotency_key=key),
        )

    async def terminate(self, participant_id: str) -> ActionRecord:
        return await self._submit(
            "terminate",
            participant_id,
            lambda client, key: client.participants.terminate(participant_id, idempotency_key=key),
        )

    async def rename(self, participant_id: str, name: str) -> ActionRecord:
        """Rename one live alias; the daemon answers directly, no operation follows."""
        return await self._submit_result(
            "rename",
            participant_id,
            lambda client, key: client.participants.update(
                participant_id, name=name, idempotency_key=key
            ),
        )

    async def spawn(
        self,
        harness: str,
        prompt: str,
        approval: str,
        *,
        cwd: str,
        action_id: str | None = None,
    ) -> ActionRecord:
        wire_prompt = prompt or None
        target_id = action_id or self._spawn_target(harness, prompt, approval, cwd)
        return await self._submit(
            "spawn",
            target_id,
            lambda client, key: client.participants.spawn(
                harness,
                wire_prompt,
                approval,
                cwd=cwd,
                idempotency_key=key,
            ),
        )

    async def resume(
        self,
        participant_id: str,
        *,
        harness: str,
        cwd: str,
        session_id: str,
        approval: str,
        prompt: str,
    ) -> ActionRecord:
        """Launch one user-selected trusted session without replaying an old spawn."""
        resume_prompt = prompt.strip() or None
        return await self._submit(
            "resume",
            participant_id,
            lambda client, key: client.participants.spawn(
                harness,
                resume_prompt,
                approval,
                cwd=cwd,
                resume=session_id,
                idempotency_key=key,
            ),
        )

    async def retry(self, action: str, target_id: str) -> ActionRecord | None:
        """Explicitly replay an uncertain visible action with its retained key."""
        identity = (action, target_id)
        record = self._records.get(identity)
        entry = self._active.get(identity)
        if entry is None:
            return record
        request = entry.request
        if record is None or request is None or record.state is not ActionState.UNCERTAIN:
            return record
        if request.kind == "result":
            return await self._invoke_result(identity, entry, request.send)
        return await self._invoke(identity, entry, request.send)

    async def refresh_pending(self) -> tuple[ActionRecord, ...]:
        """Re-observe retained accepted handles after a local connection recovers."""
        pending = tuple(
            (identity, entry)
            for identity, entry in self._active.items()
            if entry.record.operation_id is not None
            and entry.record.state in {ActionState.PENDING, ActionState.UNCERTAIN}
        )
        for identity, entry in pending:
            record = entry.record
            active_wait = entry.wait
            if active_wait is not None and not active_wait.done():
                # The old long-poll may still block on the lost connection: detach only
                # that observation and re-read; the mutation is never cancelled or replayed.
                active_wait.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await active_wait
                self._forget_wait(identity, active_wait)
            operation_id = record.operation_id
            assert operation_id is not None
            try:
                client = entry.client if entry.client is not None else self._client
                async with self._client_lock(client):
                    observed = await client.operations.get(operation_id)
            except FrontendResponseError as exc:
                record.state = ActionState.UNCERTAIN
                record.detail = (
                    f"cannot re-observe operation: {exc.value.code}: {exc.value.message}"
                )
            except FrontendTransportError as exc:
                record.state = ActionState.UNCERTAIN
                record.detail = str(exc)
            except FrontendClientError as exc:
                record.state = ActionState.UNCERTAIN
                record.detail = f"cannot re-observe operation: {exc}"
            except TypeError as exc:
                record.state = ActionState.UNCERTAIN
                record.detail = f"cannot decode operation: {exc}"
            else:
                self._apply_operation(record, observed.value)
            self._notify_changed(record)
            if self._is_terminal(record):
                await self._release_client(identity, entry)
            else:
                self._ensure_wait(identity, entry)
        return self.records

    async def _submit(
        self,
        action: str,
        target_id: str,
        request: RequestFactory,
    ) -> ActionRecord:
        identity = (action, target_id)
        existing = self.record(action, target_id)
        if existing is not None and existing.state in {ActionState.PENDING, ActionState.UNCERTAIN}:
            return existing
        record = ActionRecord(
            action,
            target_id,
            uuid4().hex,
            participant_id=None if action == "spawn" else target_id,
        )
        entry = self._prepare(identity, record, _OperationRequest("operation", request))
        return await self._invoke(identity, entry, request)

    async def _submit_result(
        self,
        action: str,
        target_id: str,
        request: ResultRequestFactory,
    ) -> ActionRecord:
        """Submit one immediately-answered mutation with the same lane discipline."""
        identity = (action, target_id)
        existing = self.record(action, target_id)
        if existing is not None and existing.state in {ActionState.PENDING, ActionState.UNCERTAIN}:
            return existing
        record = ActionRecord(action, target_id, uuid4().hex, participant_id=target_id)
        entry = self._prepare(identity, record, _ResultRequest("result", request))
        return await self._invoke_result(identity, entry, request)

    def _prepare(
        self,
        identity: ActionIdentity,
        record: ActionRecord,
        request: _OperationRequest | _ResultRequest,
    ) -> _ActiveAction:
        """Register one visible action and give it a dedicated client lane."""
        entry = _ActiveAction(record, request)
        self._records[identity] = record
        self._active[identity] = entry
        entry.client = self._acquire_client()
        return entry

    async def _invoke(
        self,
        identity: tuple[str, str],
        entry: _ActiveAction,
        request: RequestFactory,
    ) -> ActionRecord:
        accepted = await self._send_request(identity, entry, request)
        if accepted is None:
            return entry.record
        return await self._apply_acceptance(identity, entry, accepted)

    async def _invoke_result(
        self,
        identity: tuple[str, str],
        entry: _ActiveAction,
        request: ResultRequestFactory,
    ) -> ActionRecord:
        """Apply one immediately-answered mutation; no operation is observed."""
        result = await self._send_request(identity, entry, request)
        if result is None:
            return entry.record
        record = entry.record
        record.state = ActionState.SUCCEEDED
        record.result = result.value
        self._notify_changed(record)
        await self._release_client(identity, entry)
        return record

    async def _send_request[T](
        self,
        identity: tuple[str, str],
        entry: _ActiveAction,
        request: Callable[[FrontendClient, str], Awaitable[FrontendResult[T]]],
    ) -> FrontendResult[T] | None:
        """Run one admitted request on its lane; None means the record holds the outcome."""
        record = entry.record
        if self._closed:
            record.state = ActionState.UNCERTAIN
            record.detail = "Régie is closing; the action was not retried"
            await self._release_client(identity, entry)
            return None
        record.state = ActionState.PENDING
        record.detail = None
        client = entry.client if entry.client is not None else self._client
        try:
            async with self._client_lock(client):
                return await request(client, record.idempotency_key)
        except asyncio.CancelledError:
            record.state = ActionState.UNCERTAIN
            record.detail = "local wait was cancelled; accepted work was not cancelled"
            raise
        except FrontendResponseError as exc:
            record.state = ActionState.REFUSED
            record.detail = f"{exc.value.code}: {exc.value.message}"
            await self._release_client(identity, entry)
            return None
        except FrontendTransportError as exc:
            record.state = ActionState.UNCERTAIN
            record.detail = str(exc)
            return None
        except (ResponseCorrelationError, ResponseValidationError) as exc:
            record.state = ActionState.UNCERTAIN
            record.detail = f"cannot verify action response: {exc}"
            return None
        except FrontendClientError as exc:
            record.state = ActionState.REFUSED
            record.detail = str(exc)
            await self._release_client(identity, entry)
            return None
        except TypeError as exc:
            record.state = ActionState.UNCERTAIN
            record.detail = f"cannot decode action response: {exc}"
            return None

    async def _apply_acceptance(
        self,
        identity: ActionIdentity,
        entry: _ActiveAction,
        accepted: FrontendResult[AcceptedOperation],
    ) -> ActionRecord:
        """Apply one validated admission response and manage its observation lane."""
        record = entry.record
        record.operation_id = accepted.value.operation_id
        logger.info(
            "action.%s.admitted %.1fms key=%s operation=%s",
            record.action,
            (monotonic() - record.submitted_at) * 1000,
            record.idempotency_key,
            record.operation_id,
        )
        record.job_handle = accepted.value.job_handle
        if accepted.value.participant_id is not None:
            record.participant_id = accepted.value.participant_id
        if accepted.value.state == ActionState.SUCCEEDED.value:
            record.state = ActionState.SUCCEEDED
            self._notify_changed(record)
            await self._release_client(identity, entry)
            return record
        if accepted.value.state == ActionState.UNCERTAIN.value:
            record.state = ActionState.UNCERTAIN
            record.detail = "daemon accepted an operation with an uncertain outcome"
            self._notify_changed(record)
            return record
        if accepted.value.state == ActionState.FAILED.value:
            record.state = ActionState.FAILED
            record.detail = "daemon rejected the operation after admission"
            self._notify_changed(record)
            await self._release_client(identity, entry)
            return record
        if accepted.value.state in {ActionState.PENDING.value, "accepted", "running"}:
            record.state = ActionState.PENDING
            self._ensure_wait(identity, entry)
            return record
        record.state = ActionState.UNCERTAIN
        record.detail = (
            f"daemon accepted an operation with unrecognized state {accepted.value.state!r}"
        )
        return record

    def _ensure_wait(self, identity: ActionIdentity, entry: _ActiveAction) -> None:
        """Observe a reconnected accepted operation once without replaying its mutation."""
        record = entry.record
        if self._closed or record.state is not ActionState.PENDING or record.operation_id is None:
            return
        existing = entry.wait
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._wait_for_operation(identity, entry))
        entry.wait = task
        task.add_done_callback(self._wait_callback(identity, record))

    def _spawn_target(self, harness: str, prompt: str, approval: str, cwd: str) -> str:
        """Coalesce one still-visible spawn click without confusing a later new action."""
        signature = (harness, prompt, approval, cwd)
        target_id = self._spawn_targets.get(signature)
        if target_id is not None:
            record = self.record("spawn", target_id)
            if record is not None and record.state in {ActionState.PENDING, ActionState.UNCERTAIN}:
                return target_id
        target_id = f"spawn:{uuid4().hex}"
        self._spawn_targets[signature] = target_id
        return target_id

    async def _wait_for_operation(
        self,
        identity: ActionIdentity,
        entry: _ActiveAction,
    ) -> None:
        record = entry.record
        operation_id = record.operation_id
        if operation_id is None:
            return
        while not self._closed and record.state is ActionState.PENDING:
            try:
                client = entry.client if entry.client is not None else self._client
                async with self._client_lock(client):
                    observed = await client.operations.wait(operation_id, wait_seconds=30)
            except asyncio.CancelledError:
                raise
            except FrontendResponseError as exc:
                record.state = ActionState.UNCERTAIN
                record.detail = f"cannot observe operation: {exc.value.code}: {exc.value.message}"
                return
            except FrontendTransportError as exc:
                record.state = ActionState.UNCERTAIN
                record.detail = str(exc)
                return
            except FrontendClientError as exc:
                record.state = ActionState.UNCERTAIN
                record.detail = f"cannot observe operation: {exc}"
                return
            except TypeError as exc:
                record.state = ActionState.UNCERTAIN
                record.detail = f"cannot decode operation: {exc}"
                return
            if observed.value.timed_out:
                # A wait timeout only detaches this bounded observation.  Keep
                # observing the accepted durable handle; never replay its mutation.
                await asyncio.sleep(0)
                continue
            operation = observed.value.operation
            self._apply_operation(record, operation)
            self._notify_changed(record)
            if record.state is not ActionState.PENDING:
                if self._is_terminal(record):
                    await self._release_client(identity, entry)
                return
            await asyncio.sleep(0)

    @staticmethod
    def _apply_operation(record: ActionRecord, operation: Operation) -> None:
        state = operation.state
        error = operation.error
        record.phase = operation.phase or None
        record.result = operation.result
        if operation.job_handle:
            record.job_handle = operation.job_handle
        record.error_code = error.code if error is not None else None
        if state == ActionState.SUCCEEDED.value:
            record.state = ActionState.SUCCEEDED
            record.detail = None
        elif state == ActionState.UNCERTAIN.value:
            record.state = ActionState.UNCERTAIN
            record.detail = (
                error.message if error is not None else "daemon reports an uncertain outcome"
            )
        elif state == ActionState.FAILED.value:
            record.state = ActionState.FAILED
            record.detail = error.message if error is not None else "operation failed"
        elif state in {ActionState.PENDING.value, "accepted", "running"}:
            record.state = ActionState.PENDING
            record.detail = None
        else:
            record.state = ActionState.UNCERTAIN
            record.detail = f"daemon reports an unrecognized operation state {state!r}"

    def _forget_wait(self, identity: ActionIdentity, task: asyncio.Task[None]) -> None:
        entry = self._active.get(identity)
        if entry is not None and entry.wait is task:
            entry.wait = None
            self._retire_entry(identity, entry)

    def _wait_callback(
        self,
        identity: tuple[str, str],
        record: ActionRecord,
    ) -> Callable[[asyncio.Task[None]], None]:
        def forget(task: asyncio.Task[None]) -> None:
            self._forget_wait(identity, task)
            if not task.cancelled():
                self._notify_changed(record)

        return forget

    def _notify_changed(self, record: ActionRecord) -> None:
        if record.state is not ActionState.PENDING and record.observed_at is None:
            record.observed_at = monotonic()
            logger.info(
                "action.%s.observed %.1fms operation=%s state=%s",
                record.action,
                (record.observed_at - record.submitted_at) * 1000,
                record.operation_id,
                record.state.value,
            )
        if self._on_change is not None and not self._closed:
            try:
                self._on_change(record)
            except Exception:
                logger.exception("operation presentation callback failed")

    @staticmethod
    def _is_terminal(record: ActionRecord) -> bool:
        return record.state in {
            ActionState.REFUSED,
            ActionState.SUCCEEDED,
            ActionState.FAILED,
        }

    def _retire_entry(self, identity: ActionIdentity, entry: _ActiveAction) -> None:
        if (
            self._active.get(identity) is entry
            and self._is_terminal(entry.record)
            and entry.client is None
            and entry.wait is None
        ):
            self._active.pop(identity)

    async def _release_client(self, identity: ActionIdentity, entry: _ActiveAction) -> None:
        if self._is_terminal(entry.record):
            entry.request = None
            if identity[0] == "spawn":
                self._spawn_targets = {
                    signature: target
                    for signature, target in self._spawn_targets.items()
                    if target != identity[1]
                }
        client = entry.client
        entry.client = None
        self._retire_entry(identity, entry)
        if client is None or any(retained.client is client for retained in self._active.values()):
            return
        if id(client) not in self._owned_clients:
            return
        # A settled action leaves its lane idle and connected; reuse saves the next
        # action a connect and handshake.
        if not self._closed and len(self._idle_clients) < REGIE_IDLE_ACTION_CLIENTS:
            self._idle_clients.append(client)
            return
        self._owned_clients.pop(id(client))
        self._client_locks.pop(id(client), None)
        with contextlib.suppress(Exception):
            await client.close()

    def _acquire_client(self) -> FrontendClient:
        if self._idle_clients:
            return self._idle_clients.pop()
        if self._client_factory is None:
            return self._client
        client = self._client_factory()
        if client is not self._client:
            self._owned_clients[id(client)] = client
        return client

    def _client_lock(self, client: FrontendClient) -> asyncio.Lock:
        return self._client_locks.setdefault(id(client), asyncio.Lock())

    async def close(self) -> None:
        """Detach local operation waits without cancelling accepted remote work."""
        self._closed = True
        waits = tuple(entry.wait for entry in self._active.values() if entry.wait is not None)
        for wait in waits:
            wait.cancel()
        for wait in waits:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await wait
        for entry in self._active.values():
            entry.wait = None
        for client in tuple(self._owned_clients.values()):
            with contextlib.suppress(Exception):
                await client.close()
        self._owned_clients.clear()
        self._idle_clients.clear()
        self._client_locks.clear()
        self._spawn_targets.clear()
        self._active.clear()
        self._records.clear()
        self._settled.clear()


__all__ = ["ActionRecord", "ActionState", "OperationController"]
