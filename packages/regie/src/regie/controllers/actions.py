"""Durable public mutation tracking for Régie's user actions."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4

from theater.frontend import (
    AcceptedOperation,
    FrontendClient,
    FrontendClientError,
    FrontendResponseError,
    FrontendResult,
    FrontendTransportError,
)


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
    state: ActionState = ActionState.PENDING
    operation_id: str | None = None
    job_handle: str | None = None
    detail: str | None = None


type RequestFactory = Callable[[str], Awaitable[FrontendResult[AcceptedOperation]]]


class OperationController:
    """Retain one idempotency key per visible action and never replay automatically."""

    def __init__(self, client: FrontendClient) -> None:
        self._client = client
        self._records: dict[tuple[str, str], ActionRecord] = {}
        self._requests: dict[tuple[str, str], RequestFactory] = {}
        self._waits: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._spawn_targets: dict[tuple[str, str, str, str], str] = {}
        self._closed = False

    @property
    def records(self) -> tuple[ActionRecord, ...]:
        return tuple(self._records.values())

    def record(self, action: str, target_id: str) -> ActionRecord | None:
        return self._records.get((action, target_id))

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
            state=ActionState.REFUSED,
            detail=reason,
        )
        self._records[identity] = record
        return record

    async def send(self, participant_id: str, prompt: str) -> ActionRecord:
        return await self._submit(
            "send",
            participant_id,
            lambda key: self._client.controls.send(participant_id, prompt, idempotency_key=key),
        )

    async def steer(self, participant_id: str, prompt: str) -> ActionRecord:
        return await self._submit(
            "steer",
            participant_id,
            lambda key: self._client.controls.steer(participant_id, prompt, idempotency_key=key),
        )

    async def queue_followup(self, participant_id: str, prompt: str) -> ActionRecord:
        return await self._submit(
            "queue_followup",
            participant_id,
            lambda key: self._client.controls.queue_followup(
                participant_id, prompt, idempotency_key=key
            ),
        )

    async def interrupt(self, participant_id: str) -> ActionRecord:
        return await self._submit(
            "interrupt",
            participant_id,
            lambda key: self._client.controls.interrupt(participant_id, idempotency_key=key),
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
            lambda key: self._client.controls.update_settings(
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
            lambda key: self._client.participants.terminate(participant_id, idempotency_key=key),
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
            lambda key: self._client.participants.spawn(
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
        resume_prompt = prompt.strip() or "Resume the trusted prior session."
        return await self._submit(
            "resume",
            participant_id,
            lambda key: self._client.participants.spawn(
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
            operation_id = record.operation_id
            assert operation_id is not None
            try:
                observed = await self._client.operations.get(operation_id)
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
            else:
                self._apply_operation(record, observed.value.state, observed.value.error)
            self._records[identity] = record
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
        record = ActionRecord(action, target_id, uuid4().hex)
        self._records[identity] = record
        self._requests[identity] = request
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
            return record
        record.state = ActionState.PENDING
        record.detail = None
        try:
            accepted = await request(record.idempotency_key)
        except asyncio.CancelledError:
            record.state = ActionState.UNCERTAIN
            record.detail = "local wait was cancelled; accepted work was not cancelled"
            raise
        except FrontendResponseError as exc:
            record.state = ActionState.REFUSED
            record.detail = f"{exc.value.code}: {exc.value.message}"
            return record
        except FrontendTransportError as exc:
            record.state = ActionState.UNCERTAIN
            record.detail = str(exc)
            return record
        except FrontendClientError as exc:
            record.state = ActionState.REFUSED
            record.detail = str(exc)
            return record
        record.operation_id = accepted.value.operation_id
        record.job_handle = accepted.value.job_handle
        if accepted.value.state == ActionState.SUCCEEDED.value:
            record.state = ActionState.SUCCEEDED
            return record
        if accepted.value.state == ActionState.UNCERTAIN.value:
            record.state = ActionState.UNCERTAIN
            record.detail = "daemon accepted an operation with an uncertain outcome"
            return record
        if accepted.value.state == ActionState.FAILED.value:
            record.state = ActionState.FAILED
            record.detail = "daemon rejected the operation after admission"
            return record
        record.state = ActionState.PENDING
        self._ensure_wait(identity, record)
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
        task.add_done_callback(self._wait_callback(identity))

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
                observed = await self._client.operations.wait(operation_id, wait_seconds=30)
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
            if observed.value.timed_out:
                # A wait timeout only detaches this bounded observation.  Keep
                # observing the accepted durable handle; never replay its mutation.
                await asyncio.sleep(0)
                continue
            operation = observed.value.operation
            self._apply_operation(record, operation.state, operation.error)
            self._records[identity] = record
            if record.state is not ActionState.PENDING:
                return
            await asyncio.sleep(0)

    @staticmethod
    def _apply_operation(
        record: ActionRecord,
        state: str,
        error: object | None,
    ) -> None:
        if state == ActionState.SUCCEEDED.value:
            record.state = ActionState.SUCCEEDED
            record.detail = None
        elif state == ActionState.UNCERTAIN.value:
            record.state = ActionState.UNCERTAIN
            record.detail = "daemon reports an uncertain operation outcome"
        elif state == ActionState.FAILED.value:
            record.state = ActionState.FAILED
            message = getattr(error, "message", None)
            record.detail = message if isinstance(message, str) else "operation failed"
        elif state in {ActionState.PENDING.value, "accepted", "running"}:
            record.state = ActionState.PENDING
            record.detail = None
        else:
            record.state = ActionState.PENDING
            record.detail = f"daemon reports an unrecognized operation state {state!r}"

    def _forget_wait(self, identity: tuple[str, str], task: asyncio.Task[None]) -> None:
        if self._waits.get(identity) is task:
            self._waits.pop(identity, None)

    def _wait_callback(
        self,
        identity: tuple[str, str],
    ) -> Callable[[asyncio.Task[None]], None]:
        def forget(task: asyncio.Task[None]) -> None:
            self._forget_wait(identity, task)

        return forget

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


__all__ = ["ActionRecord", "ActionState", "OperationController"]
