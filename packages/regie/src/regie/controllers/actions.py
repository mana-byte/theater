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
from uuid import uuid4

from regie.constants import REGIE_ACTION_HISTORY_LIMIT, REGIE_IDLE_ACTION_CLIENTS
from theater.frontend import (
    AcceptedOperation,
    FrontendClient,
    FrontendClientError,
    FrontendResponseError,
    FrontendResult,
    FrontendTransportError,
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
        self._records: dict[tuple[str, str], ActionRecord] = {}
        self._settled: OrderedDict[tuple[str, str], ActionRecord] = OrderedDict()
        self._history_limit = history_limit
        self._requests: dict[tuple[str, str], RequestFactory] = {}
        self._clients: dict[tuple[str, str], FrontendClient] = {}
        self._owned_clients: dict[int, FrontendClient] = {}
        self._idle_clients: list[FrontendClient] = []
        self._client_locks: dict[int, asyncio.Lock] = {}
        self._waits: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._spawn_targets: dict[tuple[str, str, str, str], str] = {}
        self._closed = False

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
        existing = self._records.get(identity)
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

    async def update_settings(
        self,
        participant_id: str,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> ActionRecord:
        return await self._submit(
            "settings_update",
            participant_id,
            lambda client, key: client.controls.update_settings(
                participant_id,
                idempotency_key=key,
                **({"model": model} if model is not None else {}),
                **({"reasoning_effort": reasoning_effort} if reasoning_effort is not None else {}),
            ),
        )

    async def terminate(self, participant_id: str) -> ActionRecord:
        return await self._submit(
            "terminate",
            participant_id,
            lambda client, key: client.participants.terminate(participant_id, idempotency_key=key),
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
        request = self._requests.get(identity)
        if record is None or request is None or record.state is not ActionState.UNCERTAIN:
            return record
        return await self._invoke(identity, record, request)

    async def refresh_pending(self) -> tuple[ActionRecord, ...]:
        """Re-observe retained accepted handles after a local connection recovers."""
        pending = tuple(
            (identity, record)
            for identity, record in self._records.items()
            if record.operation_id is not None
            and record.state in {ActionState.PENDING, ActionState.UNCERTAIN}
        )
        for identity, record in pending:
            active_wait = self._waits.get(identity)
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
                client = self._clients.get(identity, self._client)
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
            self._records[identity] = record
            self._notify_changed(record)
            if self._is_terminal(record):
                await self._release_client(identity)
            else:
                self._ensure_wait(identity, record)
        return self.records

    async def _submit(
        self,
        action: str,
        target_id: str,
        request: RequestFactory,
    ) -> ActionRecord:
        identity = (action, target_id)
        existing = self._records.get(identity)
        if existing is not None and existing.state in {ActionState.PENDING, ActionState.UNCERTAIN}:
            return existing
        record = ActionRecord(
            action,
            target_id,
            uuid4().hex,
            participant_id=None if action == "spawn" else target_id,
        )
        self._records[identity] = record
        self._requests[identity] = request
        action_client = self._acquire_client()
        self._clients[identity] = action_client
        if self._client_factory is not None and action_client is not self._client:
            self._owned_clients[id(action_client)] = action_client
        return await self._invoke(identity, record, request)

    async def _invoke(
        self,
        identity: tuple[str, str],
        record: ActionRecord,
        request: RequestFactory,
    ) -> ActionRecord:
        if self._closed:
            record.state = ActionState.UNCERTAIN
            record.detail = "Régie is closing; the action was not retried"
            await self._release_client(identity)
            return record
        record.state = ActionState.PENDING
        record.detail = None
        client = self._clients.get(identity, self._client)
        try:
            async with self._client_lock(client):
                accepted = await request(client, record.idempotency_key)
        except asyncio.CancelledError:
            record.state = ActionState.UNCERTAIN
            record.detail = "local wait was cancelled; accepted work was not cancelled"
            raise
        except FrontendResponseError as exc:
            record.state = ActionState.REFUSED
            record.detail = f"{exc.value.code}: {exc.value.message}"
            await self._release_client(identity)
            return record
        except FrontendTransportError as exc:
            record.state = ActionState.UNCERTAIN
            record.detail = str(exc)
            return record
        except (ResponseCorrelationError, ResponseValidationError) as exc:
            record.state = ActionState.UNCERTAIN
            record.detail = f"cannot verify action response: {exc}"
            return record
        except FrontendClientError as exc:
            record.state = ActionState.REFUSED
            record.detail = str(exc)
            await self._release_client(identity)
            return record
        except TypeError as exc:
            record.state = ActionState.UNCERTAIN
            record.detail = f"cannot decode action response: {exc}"
            return record
        return await self._apply_acceptance(identity, record, accepted)

    async def _apply_acceptance(
        self,
        identity: tuple[str, str],
        record: ActionRecord,
        accepted: FrontendResult[AcceptedOperation],
    ) -> ActionRecord:
        """Apply one validated admission response and manage its observation lane."""
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
            await self._release_client(identity)
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
            await self._release_client(identity)
            return record
        if accepted.value.state in {ActionState.PENDING.value, "accepted", "running"}:
            record.state = ActionState.PENDING
            self._ensure_wait(identity, record)
            return record
        record.state = ActionState.UNCERTAIN
        record.detail = (
            f"daemon accepted an operation with unrecognized state {accepted.value.state!r}"
        )
        return record

    def _ensure_wait(self, identity: tuple[str, str], record: ActionRecord) -> None:
        """Observe a reconnected accepted operation once without replaying its mutation."""
        if self._closed or record.state is not ActionState.PENDING or record.operation_id is None:
            return
        existing = self._waits.get(identity)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._wait_for_operation(identity, record))
        self._waits[identity] = task
        task.add_done_callback(self._wait_callback(identity, record))

    def _spawn_target(self, harness: str, prompt: str, approval: str, cwd: str) -> str:
        """Coalesce one still-visible spawn click without confusing a later new action."""
        signature = (harness, prompt, approval, cwd)
        target_id = self._spawn_targets.get(signature)
        if target_id is not None:
            record = self._records.get(("spawn", target_id))
            if record is not None and record.state in {ActionState.PENDING, ActionState.UNCERTAIN}:
                return target_id
        target_id = f"spawn:{uuid4().hex}"
        self._spawn_targets[signature] = target_id
        return target_id

    async def _wait_for_operation(
        self,
        identity: tuple[str, str],
        record: ActionRecord,
    ) -> None:
        operation_id = record.operation_id
        if operation_id is None:
            return
        while not self._closed and record.state is ActionState.PENDING:
            try:
                client = self._clients.get(identity, self._client)
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
            self._records[identity] = record
            self._notify_changed(record)
            if record.state is not ActionState.PENDING:
                if self._is_terminal(record):
                    await self._release_client(identity)
                return
            await asyncio.sleep(0)

    @staticmethod
    def _apply_operation(record: ActionRecord, operation: object) -> None:
        state = getattr(operation, "state", "")
        error = getattr(operation, "error", None)
        phase = getattr(operation, "phase", None)
        result = getattr(operation, "result", None)
        job_handle = getattr(operation, "job_handle", None)
        record.phase = phase if isinstance(phase, str) and phase else None
        record.result = result
        if isinstance(job_handle, str) and job_handle:
            record.job_handle = job_handle
        error_code = getattr(error, "code", None)
        record.error_code = error_code if isinstance(error_code, str) else None
        if state == ActionState.SUCCEEDED.value:
            record.state = ActionState.SUCCEEDED
            record.detail = None
        elif state == ActionState.UNCERTAIN.value:
            record.state = ActionState.UNCERTAIN
            message = getattr(error, "message", None)
            record.detail = (
                message if isinstance(message, str) else "daemon reports an uncertain outcome"
            )
        elif state == ActionState.FAILED.value:
            record.state = ActionState.FAILED
            message = getattr(error, "message", None)
            record.detail = message if isinstance(message, str) else "operation failed"
        elif state in {ActionState.PENDING.value, "accepted", "running"}:
            record.state = ActionState.PENDING
            record.detail = None
        else:
            record.state = ActionState.UNCERTAIN
            record.detail = f"daemon reports an unrecognized operation state {state!r}"

    def _forget_wait(self, identity: tuple[str, str], task: asyncio.Task[None]) -> None:
        if self._waits.get(identity) is task:
            self._waits.pop(identity, None)

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

    async def _release_client(self, identity: tuple[str, str]) -> None:
        record = self._records.get(identity)
        if record is None or self._is_terminal(record):
            self._requests.pop(identity, None)
            if identity[0] == "spawn":
                self._spawn_targets = {
                    signature: target
                    for signature, target in self._spawn_targets.items()
                    if target != identity[1]
                }
        client = self._clients.pop(identity, None)
        if client is None or any(retained is client for retained in self._clients.values()):
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
        waits = tuple(self._waits.values())
        for wait in waits:
            wait.cancel()
        for wait in waits:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await wait
        self._waits.clear()
        for client in tuple(self._owned_clients.values()):
            with contextlib.suppress(Exception):
                await client.close()
        self._clients.clear()
        self._owned_clients.clear()
        self._idle_clients.clear()
        self._client_locks.clear()
        self._requests.clear()
        self._spawn_targets.clear()
        self._records.clear()
        self._settled.clear()


__all__ = ["ActionRecord", "ActionState", "OperationController"]
