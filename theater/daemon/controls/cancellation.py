"""Requester-scoped cancel of a send that provably never started."""

from __future__ import annotations

from dataclasses import dataclass

from theater.daemon.controls._common import CANCELLED_ERROR_CODE
from theater.daemon.controls._host import ControlHost
from theater.daemon.persistence.repositories.operation_predicates import (
    cancel_accepted,
    cancel_delivery_started,
    cancel_outcome_definitive,
)
from theater.harness.contracts.runtime import ControlDeliveryPhase, ControlKind, DeliveryResult
from theater.models import JobState, NotFound, NotYourJob

REASON_DELIVERY_UNCERTAIN = "delivery_uncertain"
REASON_DELIVERED = "delivered"
REASON_FINISHED = "finished"
REASON_NOT_CANCELLABLE = "not_cancellable"

_CANCELLED_RESULT = "Cancelled by its requester before delivery; nothing was sent to the target."


@dataclass(frozen=True, slots=True)
class CancelOutcome:
    handle: str
    cancelled: bool
    #: Why a cancel came too late; omitted when cancelled.
    reason: str | None = None
    #: The job's final state when it already finished.
    state: str | None = None


class CancelControls(ControlHost):
    """Cancel only work the control record proves was never transmitted."""

    async def cancel_job(self, handle: str, *, requester_id: str) -> CancelOutcome:
        job = self._store.get_job(handle)
        if job is None:
            raise NotFound(f"job {handle!r} does not exist or was already pruned")
        if requester_id not in ("cli", job.caller_id):
            raise NotYourJob(
                f"job {handle!r} was requested by {job.caller_id!r}, not you; only its "
                "requester can cancel it"
            )
        if job.kind == "spawn":
            return self._cancel_deferred_spawn(job)
        if job.kind != "send" or job.target_id is None:
            return self._cancel_outcome(handle, REASON_NOT_CANCELLABLE)
        # The dispatcher holds this lock across presence checks and transmission, so under it the
        # row is either still queued (provably untouched) or past the point of no return.
        async with self._lock(job.target_id):
            return self._cancel_locked(handle)

    def _cancel_deferred_spawn(self, job) -> CancelOutcome:
        """A spawn still waiting on its source has no worktree, argv or terminal to take back."""
        if job.state != JobState.RUNNING:
            return self._cancel_outcome(job.handle, REASON_FINISHED, state=str(job.state))
        gate = self._store.get_handoff_gate(job.handle)
        if gate is None:
            return self._cancel_outcome(job.handle, REASON_NOT_CANCELLABLE)
        if gate.state == "waiting" and self._gate_opener.cancel_unlaunched(job.handle):
            self._count_cancel("cancelled")
            return CancelOutcome(job.handle, True)
        launching = gate.state == "opening"
        return self._cancel_outcome(
            job.handle, REASON_DELIVERY_UNCERTAIN if launching else REASON_DELIVERED
        )

    def _cancel_locked(self, handle: str) -> CancelOutcome:
        job = self._store.get_job(handle)
        assert job is not None
        if job.state != JobState.RUNNING:
            return self._cancel_outcome(handle, REASON_FINISHED, state=str(job.state))
        operations = [
            op
            for op in self._store.control_operations_for_job(handle)
            if op.kind in (ControlKind.SEND, ControlKind.QUEUE_FOLLOWUP)
        ]
        queued = next(
            (op for op in operations if op.delivery_phase is ControlDeliveryPhase.QUEUED), None
        )
        if queued is None:
            if any(cancel_delivery_started(op) for op in operations):
                definitive = all(cancel_outcome_definitive(op) for op in operations)
                reason = (
                    REASON_DELIVERED
                    if definitive and any(cancel_accepted(op) for op in operations)
                    else REASON_DELIVERY_UNCERTAIN
                )
                return self._cancel_outcome(handle, reason)
            return self._cancel_outcome(handle, REASON_NOT_CANCELLABLE)
        self._store.settle_control_operation(
            queued.operation_id,
            result=DeliveryResult.REJECTED,
            error_code=CANCELLED_ERROR_CODE,
            error="cancelled by its requester before dispatch",
            updated_at=self._clock(),
        )
        self._control_notifier.notify(queued.operation_id)
        self._jobs.finish(
            handle,
            state=JobState.KILLED,
            result=_CANCELLED_RESULT,
            error_code=CANCELLED_ERROR_CODE,
        )
        self._count_cancel("cancelled")
        return CancelOutcome(handle, True)

    def _cancel_outcome(
        self, handle: str, reason: str, *, state: str | None = None
    ) -> CancelOutcome:
        self._count_cancel(reason)
        return CancelOutcome(handle, False, reason, state)
