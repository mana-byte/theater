"""Steering: amend a working participant's active native turn."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from theater.daemon.controls._common import (
    ACTION_STEER,
    CONTROL_DELIVERY_UNKNOWN,
    CONTROL_TRANSPORT_UNKNOWN,
    CONTROL_UNKNOWN_ACK_LOST,
    CONTROL_UNKNOWN_RECEIPT_MISMATCH,
    CONTROL_UNKNOWN_RECEIPT_UNKNOWN,
    DELIVERY_UNKNOWN_ERROR_CODE,
    LABEL_STEERING,
    NativeControlContext,
    NativeControlPreparation,
    _delivery_label,
    prepare_native_control,
)
from theater.daemon.controls._host import ControlHost
from theater.daemon.controls.routing import ControlRoute
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    DeliveryResult,
    RuntimeCapability,
)
from theater.models import BadRequest, Job, JobState, StaleTarget

logger = logging.getLogger("theater.daemon.controls")


@dataclass(frozen=True, slots=True)
class _SteerRequest:
    participant_id: str
    caller_id: str
    prompt: str
    job_handle: str | None
    expected_turn_id: str | None
    operation_id: str | None
    callback_operation_id: str | None
    on_reserved: Callable[[str, str | None], None] | None
    pre_reserved: bool


class SteerControls(ControlHost):
    """Amend the active turn in place."""

    async def steer(
        self,
        participant_id: str,
        *,
        caller_id: str,
        prompt: str,
        job_handle: str | None = None,
        expected_turn_id: str | None = None,
        operation_id: str | None = None,
        callback_operation_id: str | None = None,
        on_reserved: Callable[[str, str | None], None] | None = None,
        pre_reserved: bool = False,
    ) -> Job:
        """Amend exactly the current Theater job's active native turn."""
        with self._control_latency(ControlKind.STEER, participant_id) as latency:
            job, delivery = await self._steer(
                _SteerRequest(
                    participant_id,
                    caller_id,
                    prompt,
                    job_handle,
                    expected_turn_id,
                    operation_id,
                    callback_operation_id,
                    on_reserved,
                    pre_reserved,
                )
            )
            latency.delivery = delivery
            route = self.route_for(participant_id, RuntimeCapability.STEER)
            latency.transport = (
                route.transport.value if route.transport else CONTROL_TRANSPORT_UNKNOWN
            )
            return job

    async def _steer(self, request: _SteerRequest) -> tuple[Job, str]:
        """The steer body; returns its job and the delivery outcome label."""
        participant_id = request.participant_id
        runtime = self._runtime_for(participant_id)
        async with self._lock(participant_id):
            self._gates.authorize(participant_id, request.caller_id, ACTION_STEER)
            route = self.route_for(participant_id, RuntimeCapability.STEER)
            await self._require_absent(participant_id, route)
            self._gates.check_prompt(request.prompt)
            if route.is_provider:
                return await self._steer_provider(
                    participant_id,
                    route,
                    prompt=request.prompt,
                    job_handle=request.job_handle,
                    expected_turn_id=request.expected_turn_id,
                    operation_id=request.operation_id,
                    callback_operation_id=request.callback_operation_id,
                    on_reserved=request.on_reserved,
                    pre_reserved=request.pre_reserved,
                )
            if not route.is_native:
                raise self._steer_route_refusal(participant_id, route)
            prepared = await prepare_native_control(
                self,
                runtime,
                NativeControlPreparation(
                    participant_id=participant_id,
                    route_capability=RuntimeCapability.STEER,
                    required_capability=RuntimeCapability.STEER,
                    action=LABEL_STEERING,
                    refusal_label=ACTION_STEER,
                ),
            )
            return await self._steer_native(request, prepared)

    async def _steer_native(
        self, request: _SteerRequest, prepared: NativeControlContext
    ) -> tuple[Job, str]:
        participant_id = request.participant_id
        snapshot = prepared.snapshot
        expected_turn = self._require_expected_turn(
            participant_id, snapshot.native_turn_id, request.expected_turn_id
        )
        operation = self._operation_for_snapshot_turn(participant_id, snapshot)
        if operation is None or operation.job_handle is None:
            raise StaleTarget(
                f"the active native turn of participant {participant_id!r} belongs "
                "to no Theater job (a human started it in the native UI); steering "
                "refuses instead of creating a synthetic job"
            )
        if request.job_handle is not None and operation.job_handle != request.job_handle:
            raise StaleTarget(
                f"the active native turn of participant {participant_id!r} maps to "
                f"job {operation.job_handle!r}, not {request.job_handle!r}; refusing to "
                "amend a different job than the one you expect"
            )
        job = self._require_job(operation.job_handle)
        if job.state != JobState.RUNNING:
            raise StaleTarget(
                f"the active native turn of participant {participant_id!r} maps to "
                f"job {job.handle!r}, which is already {job.state}; nothing to amend"
            )
        operation_id = request.operation_id or self._mint_operation_id(
            participant_id, ControlKind.STEER
        )
        if request.pre_reserved:
            reserved = self._require_public_reservation(
                operation_id,
                participant_id=participant_id,
                kind=ControlKind.STEER,
                phase=ControlDeliveryPhase.RESERVED,
                route=prepared.route,
                job_handle=job.handle,
            )
            self._require_reserved_native_identity(reserved, snapshot)
        else:
            self._reserve(
                operation_id,
                participant_id=participant_id,
                kind=ControlKind.STEER,
                transport=ControlTransport.NATIVE_RUNTIME,
                phase=ControlDeliveryPhase.RESERVED,
                job_handle=job.handle,
                backend_generation=snapshot.backend_generation,
                native_session_id=snapshot.native_session_id,
                native_turn_id=expected_turn,
                payload=request.prompt,
            )
            self._notify_reserved(request.on_reserved, operation_id, job.handle)
        self._store.mark_control_operation_dispatched(
            operation_id,
            native_session_id=snapshot.native_session_id,
            native_turn_id=expected_turn,
            updated_at=self._clock(),
        )
        try:
            receipt = await prepared.runtime.steer(
                operation_id=operation_id,
                native_turn_id=expected_turn,
                prompt=request.prompt,
            )
        except asyncio.CancelledError:
            self._settle_uncertain(
                operation_id,
                error=(
                    "the steering control was cancelled after transmission began; "
                    "its acknowledgement is unknown and it is never retried"
                ),
            )
            self._count_unknown_delivery(ControlKind.STEER, CONTROL_UNKNOWN_ACK_LOST)
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
                "steer delivery for %s job %s is uncertain: %s",
                participant_id,
                job.handle,
                exc,
            )
            self._count_unknown_delivery(ControlKind.STEER, CONTROL_UNKNOWN_ACK_LOST)
            return job, CONTROL_DELIVERY_UNKNOWN
        if not self._receipt_names_operation(operation_id, receipt):
            self._settle_uncertain(
                operation_id,
                error=(
                    f"the steering receipt named operation {receipt.operation_id!r}, "
                    f"not {operation_id!r}; the amendment is uncertain and the "
                    "receipt is not trusted to settle it"
                ),
            )
            self._count_unknown_delivery(ControlKind.STEER, CONTROL_UNKNOWN_RECEIPT_MISMATCH)
            return job, CONTROL_DELIVERY_UNKNOWN
        self._settle_from_receipt(operation_id, receipt)
        if receipt.result is DeliveryResult.REJECTED:
            detail = receipt.error or "the expected turn is no longer active"
            raise StaleTarget(
                f"participant {participant_id!r} refused the steering amendment "
                f"({receipt.error_code or 'rejected'}: {detail}); the refusal is "
                "final — queue a followup instead of retrying the steer"
            )
        if receipt.result is DeliveryResult.UNKNOWN:
            logger.warning(
                "steer delivery for %s job %s stayed uncertain",
                participant_id,
                job.handle,
            )
            self._count_unknown_delivery(ControlKind.STEER, CONTROL_UNKNOWN_RECEIPT_UNKNOWN)
        return job, _delivery_label(receipt.result)

    async def _steer_provider(
        self,
        participant_id: str,
        route: ControlRoute,
        *,
        prompt: str,
        job_handle: str | None,
        expected_turn_id: str | None,
        operation_id: str | None,
        callback_operation_id: str | None,
        on_reserved: Callable[[str, str | None], None] | None,
        pre_reserved: bool,
    ) -> tuple[Job, str]:
        jobs = self._store.active_running_jobs_for_target(participant_id)
        if len(jobs) != 1:
            raise StaleTarget(
                f"participant {participant_id!r} does not have exactly one active Theater job "
                "to steer"
            )
        job = jobs[0]
        if job_handle is not None and job.handle != job_handle:
            raise StaleTarget(
                f"participant {participant_id!r} is running job {job.handle!r}, not {job_handle!r}"
            )
        terminal = self._provider.require(participant_id, RuntimeCapability.STEER, route)
        observed_turn = terminal.occupant_evidence.get("turn_id")
        if expected_turn_id is not None and observed_turn != expected_turn_id:
            raise StaleTarget(
                f"participant {participant_id!r} no longer reports expected turn "
                f"{expected_turn_id!r}"
            )
        control_id = operation_id or self._mint_operation_id(participant_id, ControlKind.STEER)
        if pre_reserved:
            self._require_public_reservation(
                control_id,
                participant_id=participant_id,
                kind=ControlKind.STEER,
                phase=ControlDeliveryPhase.RESERVED,
                route=route,
                job_handle=job.handle,
            )
        else:
            self._provider.reserve(
                control_id,
                route,
                participant_id=participant_id,
                kind=ControlKind.STEER,
                phase=ControlDeliveryPhase.RESERVED,
                job_handle=job.handle,
                payload=prompt,
            )
            self._notify_reserved(on_reserved, control_id, job.handle)
        result = await self._provider.deliver(
            route,
            capability=RuntimeCapability.STEER,
            kind=ControlKind.STEER,
            participant_id=participant_id,
            control_operation_id=control_id,
            callback_operation_id=callback_operation_id or control_id,
            action={"kind": "paste_text", "text": prompt},
            job_handle=None,
        )
        return job, _delivery_label(result)

    @staticmethod
    def _steer_route_refusal(participant_id: str, route: ControlRoute) -> BadRequest:
        if not route.native_wiring:
            return BadRequest(
                f"steering participant {participant_id!r} requires native runtime wiring; "
                "its harness has no runtime, so the prompt can only be sent with the "
                "ordinary idle-guarded send or queued as a followup"
            )
        return BadRequest(
            f"steering participant {participant_id!r} is unavailable on its selected transport"
        )

    @staticmethod
    def _require_expected_turn(
        participant_id: str, actual: str | None, expected: str | None
    ) -> str:
        if actual is None:
            raise StaleTarget(f"participant {participant_id!r} has no active native turn to steer")
        if expected is not None and expected != actual:
            raise StaleTarget(
                f"participant {participant_id!r} is on native turn {actual!r}, "
                f"not expected turn {expected!r}"
            )
        return actual
