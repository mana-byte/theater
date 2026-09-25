"""Interrupts and followup cancellation, including control transfer."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace

from theater.daemon.controls._common import (
    ACTION_INTERRUPT,
    CONTROL_DELIVERY_ACCEPTED,
    CONTROL_DELIVERY_REJECTED,
    CONTROL_DELIVERY_UNKNOWN,
    CONTROL_TRANSFERRED_ERROR_CODE,
    CONTROL_TRANSPORT_UNKNOWN,
    CONTROL_UNKNOWN_ACK_LOST,
    CONTROL_UNKNOWN_RECEIPT_MISMATCH,
    DELIVERY_UNKNOWN_ERROR_CODE,
    INTERRUPTED_ERROR_CODE,
    LABEL_INTERRUPTION,
    NativeControlContext,
    NativeControlPreparation,
    prepare_native_control,
)
from theater.daemon.controls._host import ControlHost
from theater.daemon.controls.provider_interrupt import interrupt_action
from theater.daemon.controls.routing import ControlRoute
from theater.daemon.persistence.repositories.control_operations import ControlOperation
from theater.daemon.persistence.repositories.native_evidence import NativeTerminalEvidence
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    DeliveryResult,
    RuntimeCapability,
)
from theater.models import BadRequest, Job, JobState

logger = logging.getLogger("theater.daemon.controls")


@dataclass(frozen=True, slots=True)
class InterruptOutcome:
    """What one interrupt did."""

    #: Whether an interruption was requested and accepted.
    interrupted: bool
    #: ``already_idle`` when there was no active turn to interrupt.
    reason: str | None = None
    #: Job handles cancelled out of the queue before they were ever delivered.
    cancelled_followups: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _InterruptRequest:
    participant_id: str
    caller_id: str
    operation_id: str | None
    callback_operation_id: str | None
    on_reserved: Callable[[str, str | None], None] | None
    pre_reserved: bool


class InterruptControls(ControlHost):
    """Interrupt active turns and cancel undelivered followups."""

    async def interrupt(
        self,
        participant_id: str,
        *,
        caller_id: str,
        operation_id: str | None = None,
        callback_operation_id: str | None = None,
        on_reserved: Callable[[str, str | None], None] | None = None,
        pre_reserved: bool = False,
    ) -> InterruptOutcome:
        """Cancel every undelivered followup, then interrupt the active turn."""
        with self._control_latency(ControlKind.INTERRUPT, participant_id) as latency:
            outcome = await self._interrupt(
                _InterruptRequest(
                    participant_id,
                    caller_id,
                    operation_id,
                    callback_operation_id,
                    on_reserved,
                    pre_reserved,
                )
            )
            # Only an accepted receipt confirms interruption; UNKNOWN remains delivery_unknown.
            latency.delivery = (
                CONTROL_DELIVERY_ACCEPTED
                if outcome.interrupted
                else (
                    CONTROL_DELIVERY_UNKNOWN
                    if outcome.reason == DELIVERY_UNKNOWN_ERROR_CODE
                    else CONTROL_DELIVERY_REJECTED
                )
            )
            route = self.route_for(participant_id, RuntimeCapability.INTERRUPT)
            latency.transport = (
                route.transport.value if route.transport else CONTROL_TRANSPORT_UNKNOWN
            )
            return outcome

    async def _interrupt(self, request: _InterruptRequest) -> InterruptOutcome:
        """The interrupt body."""
        participant_id = request.participant_id
        runtime = self._runtime_for(participant_id)
        async with self._lock(participant_id):
            self._gates.authorize(participant_id, request.caller_id, ACTION_INTERRUPT)
            route = self.route_for(participant_id, RuntimeCapability.INTERRUPT)
            await self._require_absent(participant_id, route)
            if route.is_provider:
                return await self._interrupt_provider(request, route)
            if not route.is_native:
                if not route.native_wiring:
                    raise BadRequest(
                        f"interrupting participant {participant_id!r} through the control "
                        "service requires native runtime wiring; its harness uses the "
                        "existing pane-interrupt path"
                    )
                raise BadRequest(
                    f"interrupting participant {participant_id!r} is unavailable on its selected "
                    "transport"
                )
            prepared = await prepare_native_control(
                self,
                runtime,
                NativeControlPreparation(
                    participant_id=participant_id,
                    route_capability=RuntimeCapability.INTERRUPT,
                    required_capability=RuntimeCapability.INTERRUPT,
                    action=LABEL_INTERRUPTION,
                    refusal_label=ACTION_INTERRUPT,
                ),
            )
            return await self._interrupt_native(request, prepared)

    async def _interrupt_provider(
        self, request: _InterruptRequest, route: ControlRoute
    ) -> InterruptOutcome:
        participant_id = request.participant_id
        self._provider.require(participant_id, RuntimeCapability.INTERRUPT, route)
        cancelled = await self._cancel_queued_followups(participant_id)
        action = interrupt_action(self._store, self._gates, participant_id)
        if action is None:
            if request.pre_reserved and request.operation_id is not None:
                self._store.settle_control_operation(
                    request.operation_id,
                    result=DeliveryResult.ACCEPTED,
                    error_code="already_idle",
                    error="participant had no active work to interrupt",
                    updated_at=self._clock(),
                )
            return InterruptOutcome(
                interrupted=False, reason="already_idle", cancelled_followups=cancelled
            )
        control_id = request.operation_id or self._mint_operation_id(
            participant_id, ControlKind.INTERRUPT
        )
        if request.pre_reserved:
            self._require_public_reservation(
                control_id,
                participant_id=participant_id,
                kind=ControlKind.INTERRUPT,
                phase=ControlDeliveryPhase.RESERVED,
                route=route,
            )
        else:
            self._provider.reserve(
                control_id,
                route,
                participant_id=participant_id,
                kind=ControlKind.INTERRUPT,
                phase=ControlDeliveryPhase.RESERVED,
            )
            self._notify_reserved(request.on_reserved, control_id, None)
        result = await self._provider.deliver(
            route,
            capability=RuntimeCapability.INTERRUPT,
            kind=ControlKind.INTERRUPT,
            participant_id=participant_id,
            control_operation_id=control_id,
            callback_operation_id=request.callback_operation_id or control_id,
            action=action,
            job_handle=None,
        )
        return InterruptOutcome(
            interrupted=result is DeliveryResult.ACCEPTED,
            reason=(
                None
                if result is DeliveryResult.ACCEPTED
                else DELIVERY_UNKNOWN_ERROR_CODE
                if result is DeliveryResult.UNKNOWN
                else "refused"
            ),
            cancelled_followups=cancelled,
        )

    async def _interrupt_native(
        self, request: _InterruptRequest, prepared: NativeControlContext
    ) -> InterruptOutcome:
        participant_id = request.participant_id
        snapshot = prepared.snapshot
        cancelled = await self._cancel_queued_followups(participant_id)
        turn = snapshot.native_turn_id
        if turn is None:
            if request.pre_reserved and request.operation_id is not None:
                self._store.settle_control_operation(
                    request.operation_id,
                    result=DeliveryResult.ACCEPTED,
                    error_code="already_idle",
                    error="participant had no active native turn to interrupt",
                    updated_at=self._clock(),
                )
            return InterruptOutcome(
                interrupted=False, reason="already_idle", cancelled_followups=cancelled
            )
        operation_id = request.operation_id or self._mint_operation_id(
            participant_id, ControlKind.INTERRUPT
        )
        if request.pre_reserved:
            reserved = self._require_public_reservation(
                operation_id,
                participant_id=participant_id,
                kind=ControlKind.INTERRUPT,
                phase=ControlDeliveryPhase.RESERVED,
                route=prepared.route,
            )
            self._require_reserved_native_identity(reserved, snapshot)
        else:
            self._reserve(
                operation_id,
                participant_id=participant_id,
                kind=ControlKind.INTERRUPT,
                transport=ControlTransport.NATIVE_RUNTIME,
                phase=ControlDeliveryPhase.RESERVED,
                backend_generation=snapshot.backend_generation,
                native_session_id=snapshot.native_session_id,
                native_turn_id=turn,
            )
            self._notify_reserved(request.on_reserved, operation_id, None)
        self._store.mark_control_operation_dispatched(
            operation_id,
            native_session_id=snapshot.native_session_id,
            native_turn_id=turn,
            updated_at=self._clock(),
        )
        return await self._deliver_native_interrupt(
            prepared, operation_id, participant_id, turn, cancelled
        )

    async def _deliver_native_interrupt(
        self,
        prepared: NativeControlContext,
        operation_id: str,
        participant_id: str,
        turn: str,
        cancelled: tuple[str, ...],
    ) -> InterruptOutcome:
        try:
            receipt = await prepared.runtime.interrupt(
                operation_id=operation_id, native_turn_id=turn
            )
        except asyncio.CancelledError:
            self._settle_uncertain(
                operation_id,
                error=(
                    "the interruption control was cancelled after transmission began; "
                    "its acknowledgement is unknown and it is never retried"
                ),
            )
            self._count_unknown_delivery(ControlKind.INTERRUPT, CONTROL_UNKNOWN_ACK_LOST)
            raise
        except Exception as exc:
            self._store.settle_control_operation(
                operation_id,
                result=DeliveryResult.UNKNOWN,
                error_code=DELIVERY_UNKNOWN_ERROR_CODE,
                error=str(exc),
                updated_at=self._clock(),
            )
            logger.warning(
                "interrupt delivery for %s turn %s is uncertain: %s",
                participant_id,
                turn,
                exc,
            )
            self._count_unknown_delivery(ControlKind.INTERRUPT, CONTROL_UNKNOWN_ACK_LOST)
            return InterruptOutcome(
                interrupted=False,
                reason=DELIVERY_UNKNOWN_ERROR_CODE,
                cancelled_followups=cancelled,
            )
        if not self._receipt_names_operation(operation_id, receipt):
            self._settle_uncertain(
                operation_id,
                error=(
                    f"the interrupt receipt named operation {receipt.operation_id!r}, "
                    f"not {operation_id!r}; the interruption is uncertain and the "
                    "receipt is not trusted to settle it"
                ),
            )
            self._count_unknown_delivery(ControlKind.INTERRUPT, CONTROL_UNKNOWN_RECEIPT_MISMATCH)
            return InterruptOutcome(
                interrupted=False,
                reason=DELIVERY_UNKNOWN_ERROR_CODE,
                cancelled_followups=cancelled,
            )
        self._settle_from_receipt(operation_id, receipt)
        if receipt.result is not DeliveryResult.ACCEPTED:
            return InterruptOutcome(
                interrupted=False,
                reason=receipt.error_code or "refused",
                cancelled_followups=cancelled,
            )
        return InterruptOutcome(interrupted=True, cancelled_followups=cancelled)

    async def handle_native_ui_interrupt(
        self, participant_id: str, *, native_turn_id: str | None = None
    ) -> tuple[str, ...]:
        """A native-UI-initiated interruption: cancel the pending queue."""
        del native_turn_id  # the exact turn is already gone; nothing to request
        return await self.cancel_queued_followups(participant_id)

    async def cancel_queued_followups(self, participant_id: str) -> tuple[str, ...]:
        """Cancel every undelivered queued followup; return the cancelled handles.

        The queue is Theater-owned, so every transport's cancel ends ``killed``/``interrupted`` and
        the post-interrupt idle transition finds nothing left to dispatch.
        """
        async with self._lock(participant_id):
            return await self._cancel_queued_followups(participant_id)

    @asynccontextmanager
    async def hold_participant_locks(self, participant_ids: Iterable[str]) -> AsyncIterator[None]:
        """Hold participant schedulers in stable order for an atomic batch."""
        locks = [self._lock(participant_id) for participant_id in sorted(set(participant_ids))]
        acquired: list[asyncio.Lock] = []
        try:
            for lock in locks:
                await lock.acquire()
                acquired.append(lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()

    def cancel_queued_for_control_transfer(
        self,
        participant_ids: Sequence[str],
        *,
        unit,
        timestamp: float,
    ) -> tuple[Job, ...]:
        """Cancel only undispatched followups inside the ownership write unit."""
        cancelled: list[Job] = []
        for participant_id in participant_ids:
            operations = self._store.queued_control_operations(
                participant_id, connection=unit.connection
            )
            for operation in operations:
                self._store.settle_control_operation(
                    operation.operation_id,
                    result=DeliveryResult.REJECTED,
                    error_code=CONTROL_TRANSFERRED_ERROR_CODE,
                    error=(
                        "queued followup was cancelled before dispatch because control "
                        "ownership changed"
                    ),
                    updated_at=timestamp,
                    connection=unit.connection,
                )
                unit.after_commit(
                    lambda operation_id=operation.operation_id: self.notify_persisted_settlement(
                        operation_id
                    )
                )
                if operation.job_handle is None:
                    continue
                job = self._store.get_job(operation.job_handle, connection=unit.connection)
                if job is None or job.state != JobState.RUNNING:
                    continue
                finished = replace(
                    job,
                    state=JobState.KILLED.value,
                    result=(
                        "Queued followup was cancelled before dispatch because control "
                        "ownership changed. Queue it again under the new owner if needed."
                    ),
                    error_code=CONTROL_TRANSFERRED_ERROR_CODE,
                    finished_at=timestamp,
                )
                self._store.finish_job(
                    job.handle,
                    state=finished.state,
                    result=finished.result,
                    error_code=finished.error_code,
                    finished_at=finished.finished_at,
                    response_format=finished.response_format,
                    structured_result=finished.structured_result,
                    structured_status=finished.structured_status,
                    connection=unit.connection,
                )
                unit.after_commit(
                    lambda handle=job.handle: self._jobs.finish(
                        handle,
                        state=JobState.KILLED,
                        error_code=CONTROL_TRANSFERRED_ERROR_CODE,
                    )
                )
                cancelled.append(finished)
        return tuple(cancelled)

    async def _cancel_queued_followups(self, participant_id: str) -> tuple[str, ...]:
        """Durably cancel every queued followup; return the cancelled handles."""
        return self._cancel_pending_followups(participant_id)

    def _cancel_pending_followups(
        self, participant_id: str, evidence: NativeTerminalEvidence | None = None
    ) -> tuple[str, ...]:
        """Cancel only the pending work causally covered by native evidence."""
        cancelled: list[str] = []
        for operation in self._store.queued_control_operations(participant_id):
            if evidence is not None and not self._interruption_covers(operation, evidence):
                continue
            self._store.settle_control_operation(
                operation.operation_id,
                result=DeliveryResult.REJECTED,
                error_code=INTERRUPTED_ERROR_CODE,
                error="interrupted before dispatch; the active turn was interrupted",
                updated_at=self._clock(),
            )
            self._control_notifier.notify(operation.operation_id)
            handle = operation.job_handle or ""
            if operation.job_handle:
                self._jobs.finish(
                    operation.job_handle,
                    state=JobState.KILLED,
                    result=(
                        "Queued followup was cancelled by interrupt before it was "
                        "delivered; it was never sent to the participant. Queue it "
                        "again if the work is still wanted."
                    ),
                    error_code=INTERRUPTED_ERROR_CODE,
                )
                cancelled.append(handle)
        return tuple(cancelled)

    @staticmethod
    def _interruption_covers(operation: ControlOperation, evidence: NativeTerminalEvidence) -> bool:
        if (
            operation.transport is not ControlTransport.NATIVE_RUNTIME
            or operation.backend_generation != evidence.backend_generation
            or operation.native_session_id != evidence.native_session_id
        ):
            return False
        if not evidence.from_history:
            return True
        if evidence.completed_at is not None:
            if operation.created_at < evidence.completed_at:
                return True
            # Native history timestamps can have one-second precision.
            if operation.created_at >= evidence.completed_at + 1:
                return False
        if operation.payload is None:
            return False
        try:
            payload = json.loads(operation.payload)
        except (TypeError, ValueError):
            return False
        return (
            isinstance(payload, dict)
            and payload.get("queue_predecessor_turn") == evidence.native_turn_id
        )
