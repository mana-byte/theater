"""Native runtime delivery and receipt settlement."""

from __future__ import annotations

import asyncio
import logging

from theater.daemon.controls._common import (
    CONTROL_UNKNOWN_ACK_LOST,
    CONTROL_UNKNOWN_RECEIPT_MISMATCH,
    CONTROL_UNKNOWN_RECEIPT_UNKNOWN,
    CONTROL_UNKNOWN_UNCORRELATED,
    DELIVERY_UNKNOWN_ERROR_CODE,
    NATIVE_TURN_CONFLICT_ERROR_CODE,
    SEND_REJECTED_ERROR_CODE,
)
from theater.daemon.controls._host import ControlHost
from theater.daemon.events.publication import control_event, next_revision
from theater.daemon.persistence.repositories.control_operations import (
    ControlOperationAmbiguityError,
)
from theater.harness.contracts.runtime import (
    ControlKind,
    ControlReceipt,
    DeliveryResult,
    HarnessRuntime,
    RuntimeCapability,
    RuntimeSnapshot,
)
from theater.models import BadRequest, JobState

logger = logging.getLogger("theater.daemon.controls")


class NativeDeliveryControls(ControlHost):
    """Deliver prompts natively and settle their receipts."""

    async def _deliver_native(
        self,
        runtime: HarnessRuntime,
        *,
        kind: ControlKind,
        participant_id: str,
        operation_id: str,
        prompt: str,
        job_handle: str,
        snapshot: RuntimeSnapshot,
    ) -> DeliveryResult | None:
        """DISPATCHED before transmission; settle from the receipt; no retry."""
        operation = self._store.get_control_operation(operation_id)
        if operation is None:
            raise RuntimeError(f"native control reservation {operation_id!r} disappeared")
        self._require_reserved_native_identity(operation, snapshot)
        self._store.mark_control_operation_dispatched(
            operation_id,
            native_session_id=snapshot.native_session_id,
            execution_barrier=True,
            updated_at=self._clock(),
        )
        # Arm durable reconciliation before the runtime write.
        self._schedule_maintenance(participant_id)
        try:
            receipt = await runtime.send(operation_id=operation_id, prompt=prompt)
        except asyncio.CancelledError:
            # ``turn/start`` may have crossed the transport write before the caller's cancellation
            # arrived.
            self._settle_uncertain(
                operation_id,
                execution_barrier=True,
                error=(
                    "the prompt delivery was cancelled after transmission began; "
                    "its acknowledgement is unknown and it is never retried"
                ),
            )
            self._count_unknown_delivery(kind, CONTROL_UNKNOWN_ACK_LOST)
            self._schedule_maintenance(participant_id)
            raise
        except Exception as exc:
            logger.warning(
                "delivery of %s to %s is uncertain (acknowledgement lost): %s; "
                "no retry, no tmux fallback",
                operation_id,
                participant_id,
                exc,
            )
            self._count_unknown_delivery(kind, CONTROL_UNKNOWN_ACK_LOST)
            self._schedule_maintenance(participant_id)
            return None
        if not self._receipt_names_operation(operation_id, receipt):
            # Mismatched receipts leave delivery UNKNOWN until bounded reconciliation resolves it.
            self._settle_uncertain(
                operation_id,
                execution_barrier=True,
                error=(
                    f"the native receipt named operation {receipt.operation_id!r}, "
                    f"not {operation_id!r}; the delivery is uncertain and the "
                    "receipt is not trusted to settle it"
                ),
            )
            self._count_unknown_delivery(kind, CONTROL_UNKNOWN_RECEIPT_MISMATCH)
            self._schedule_maintenance(participant_id)
            return DeliveryResult.UNKNOWN
        if receipt.result is DeliveryResult.REJECTED:
            self._settle_from_receipt(operation_id, receipt, execution_barrier=False)
            self._jobs.finish(
                job_handle,
                state=JobState.CRASHED,
                result=receipt.error or "the native backend refused the prompt",
                error_code=receipt.error_code or SEND_REJECTED_ERROR_CODE,
            )
            return DeliveryResult.REJECTED
        if receipt.result is DeliveryResult.ACCEPTED:
            if receipt.native_turn_id is None:
                # Accepted but uncorrelated: without a native turn id the job can never be finished
                # by evidence.
                self._settle_uncertain(
                    operation_id,
                    execution_barrier=True,
                    error=(
                        "the native backend accepted the prompt but reported no "
                        "native turn id; the accepted turn cannot be correlated, "
                        "so the delivery stays uncertain"
                    ),
                )
                self._count_unknown_delivery(kind, CONTROL_UNKNOWN_UNCORRELATED)
                self._schedule_maintenance(participant_id)
                return DeliveryResult.UNKNOWN
            # Check before binding: two jobs sharing one exact native turn make completion
            # ambiguous.
            if self._turn_is_bound_to_another_job(participant_id, snapshot, receipt, job_handle):
                self._store.settle_control_operation(
                    operation_id,
                    result=DeliveryResult.REJECTED,
                    error_code=NATIVE_TURN_CONFLICT_ERROR_CODE,
                    error=(
                        f"native turn {receipt.native_turn_id!r} is already "
                        "bound to another Theater job"
                    ),
                    execution_barrier=False,
                    updated_at=self._clock(),
                )
                self._jobs.finish(
                    job_handle,
                    state=JobState.CRASHED,
                    result=(
                        f"the native backend reported turn {receipt.native_turn_id!r}, "
                        "which is already bound to another Theater job; refusing "
                        "to bind two jobs to one native turn"
                    ),
                    error_code=NATIVE_TURN_CONFLICT_ERROR_CODE,
                )
                return DeliveryResult.REJECTED
            with self._store.write_unit() as unit:
                connection = unit.connection
                self._settle_from_receipt(
                    operation_id, receipt, execution_barrier=False, connection=connection
                )
                self._bind_queued_predecessor(
                    participant_id, snapshot, receipt.native_turn_id, connection=connection
                )
                settled = self._store.get_control_operation(operation_id, connection=connection)
                assert settled is not None
                event = control_event(
                    self._store,
                    settled,
                    connection,
                    revision=next_revision(self._store, connection),
                )
                if event is not None:
                    self._store.journal.append_group(unit, [event])
            return DeliveryResult.ACCEPTED
        # An uncertain delivery settles UNKNOWN with the turn it named, if any: never retried, never
        # tmux-fallback, eligible only for exact evidence or snapshot reconciliation.
        self._settle_from_receipt(operation_id, receipt, execution_barrier=True)
        if receipt.result is DeliveryResult.UNKNOWN and snapshot.native_session_id is not None:
            logger.warning(
                "delivery of %s to %s stayed uncertain (turn %s); no retry, "
                "no tmux fallback — evidence or the snapshot is the only path",
                operation_id,
                participant_id,
                receipt.native_turn_id,
            )
        self._count_unknown_delivery(kind, CONTROL_UNKNOWN_RECEIPT_UNKNOWN)
        self._schedule_maintenance(participant_id)
        return receipt.result

    def _receipt_names_operation(self, operation_id: str, receipt: ControlReceipt) -> bool:
        """A receipt is authoritative only for the operation it names."""
        if receipt.operation_id == operation_id:
            return True
        logger.error(
            "native receipt named operation %r, expected %r; the receipt is not "
            "trusted to settle the reserved operation",
            receipt.operation_id,
            operation_id,
        )
        return False

    def _settle_uncertain(
        self,
        operation_id: str,
        *,
        error: str,
        execution_barrier: bool | None = None,
    ) -> None:
        """Settle one operation as uncertain: never retried, never fallback."""
        self._store.settle_control_operation(
            operation_id,
            result=DeliveryResult.UNKNOWN,
            error_code=DELIVERY_UNKNOWN_ERROR_CODE,
            error=error,
            execution_barrier=execution_barrier,
            updated_at=self._clock(),
        )

    def _settle_from_receipt(
        self,
        operation_id: str,
        receipt: ControlReceipt,
        *,
        execution_barrier: bool | None = None,
        connection=None,
    ) -> None:
        self._store.settle_control_operation(
            operation_id,
            result=receipt.result,
            native_turn_id=receipt.native_turn_id,
            error_code=receipt.error_code,
            error=receipt.error,
            execution_barrier=execution_barrier,
            updated_at=self._clock(),
            connection=connection,
        )

    def _turn_is_bound_to_another_job(
        self,
        participant_id: str,
        snapshot: RuntimeSnapshot,
        receipt: ControlReceipt,
        job_handle: str,
    ) -> bool:
        """Never bind two Theater jobs to one native turn; fail closed."""
        turn = receipt.native_turn_id
        if turn is None or snapshot.native_session_id is None:
            return False
        try:
            operation = self._store.control_operation_for_native_turn(
                participant_id=participant_id,
                backend_generation=snapshot.backend_generation,
                native_session_id=snapshot.native_session_id,
                native_turn_id=turn,
            )
        except ControlOperationAmbiguityError:
            logger.error(  # noqa: TRY400 - a controlled fail-closed, not a crash
                "native turn %s of %s already maps to multiple job-bearing "
                "operations; failing job %s closed instead of settling into "
                "an ambiguous mapping",
                turn,
                participant_id,
                job_handle,
            )
            return True
        if operation is None or operation.job_handle == job_handle:
            return False
        logger.error(
            "native turn %s of %s is already bound to job %s; failing job %s "
            "closed instead of binding two jobs to one turn",
            turn,
            participant_id,
            operation.job_handle,
            job_handle,
        )
        return True

    def _require_capability(
        self,
        participant_id: str,
        snapshot: RuntimeSnapshot,
        capability: RuntimeCapability,
        action: str,
    ) -> None:
        """Fails-closed capability gate at execution; no fallback ever."""
        if snapshot.capabilities.supports(capability):
            return
        reason = snapshot.capabilities.reason_for(capability)
        raise BadRequest(
            f"participant {participant_id!r} does not support {action} "
            f"({reason}); the native runtime gates this capability, so the "
            "control is refused — never retried, never fallen back"
        )
