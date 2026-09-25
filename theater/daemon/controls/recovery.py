"""Restart recovery and ambiguous-delivery reconciliation."""

from __future__ import annotations

import logging
import math

from theater.daemon.controls._common import (
    AMBIGUOUS_DELIVERY_DEADLINE_SECONDS,
    CONTROL_UNKNOWN_DEADLINE,
    CONTROL_UNKNOWN_RESTART,
    DAEMON_RESTARTED_ERROR_CODE,
    DELIVERY_UNKNOWN_ERROR_CODE,
    SEND_REJECTED_ERROR_CODE,
)
from theater.daemon.controls._host import ControlHost
from theater.daemon.persistence.repositories.control_operations import ControlOperation
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    DeliveryResult,
    RuntimeCapability,
    RuntimeExecutionState,
    RuntimeSnapshot,
)
from theater.models import Job, JobState

logger = logging.getLogger("theater.daemon.controls")

#: The job result for a prompt that never began transmission before the
#: daemon restart: failed, never replayed, and safe to send again.
_UNDELIVERED_RESTART_RESULT = (
    "Send failed: the Theater daemon restarted before the prompt was "
    "transmitted, and an undelivered prompt is never replayed. Send it "
    "again if the work is still wanted."
)


class RecoveryControls(ControlHost):
    """Fail undelivered work and reconcile uncertain deliveries."""

    def fail_undelivered_followups(
        self,
        participant_ids: list[str],
        *,
        error_code: str = DAEMON_RESTARTED_ERROR_CODE,
        preserve_legacy_queued: bool = False,
    ) -> list[Job]:
        """Settle restart residue without replaying possibly delivered work."""
        failed: list[Job] = []
        for participant_id in participant_ids:
            failed.extend(self._fail_reserved_operations(participant_id, error_code))
            self._settle_non_prompt_dispatched(participant_id)
            failed.extend(
                self._fail_queued_followups(
                    participant_id,
                    error_code,
                    preserve_legacy_queued=preserve_legacy_queued,
                )
            )
            failed.extend(self._reconcile_running_jobs_at_restart(participant_id, error_code))
        return failed

    def _fail_reserved_operations(self, participant_id: str, error_code: str) -> list[Job]:
        """Settle every RESERVED operation — job-bearing and jobless."""
        failed: list[Job] = []
        for operation in self._store.control_operations_in_phases(
            participant_id, (ControlDeliveryPhase.RESERVED,)
        ):
            self._store.settle_control_operation(
                operation.operation_id,
                result=DeliveryResult.REJECTED,
                error_code=error_code,
                error=(
                    (
                        "the Theater daemon restarted before this "
                        f"{operation.kind.value} operation was transmitted; it "
                        "is definitively never delivered and never retried"
                    )
                    if operation.job_handle is None
                    else (
                        "the Theater daemon restarted before transmission "
                        "began; the delivery is never retried"
                    )
                ),
                updated_at=self._clock(),
            )
            if operation.kind not in (ControlKind.SEND, ControlKind.QUEUE_FOLLOWUP):
                continue
            if operation.job_handle is not None:
                current = self._store.get_job(operation.job_handle)
                if current is not None and current.state == JobState.RUNNING:
                    failed_job = self._jobs.finish(
                        operation.job_handle,
                        state=JobState.CRASHED,
                        result=_UNDELIVERED_RESTART_RESULT,
                        error_code=error_code,
                    )
                    if failed_job is not None:
                        failed.append(failed_job)
        return [job for job in failed if job is not None]

    def _settle_non_prompt_dispatched(self, participant_id: str) -> None:
        """Settle every non-prompt DISPATCHED operation as ``unknown``."""
        for operation in self._store.control_operations_in_phases(
            participant_id, (ControlDeliveryPhase.DISPATCHED,)
        ):
            if operation.kind in (ControlKind.SEND, ControlKind.QUEUE_FOLLOWUP):
                continue
            self._store.settle_control_operation(
                operation.operation_id,
                result=DeliveryResult.UNKNOWN,
                error_code=DELIVERY_UNKNOWN_ERROR_CODE,
                error=(
                    "the Theater daemon restarted after this "
                    f"{operation.kind.value} operation's transmission began; "
                    "its acknowledgement is unknown and it is never "
                    "retried"
                ),
                updated_at=self._clock(),
            )
            self._count_unknown_delivery(operation.kind, CONTROL_UNKNOWN_RESTART)

    def _fail_queued_followups(
        self,
        participant_id: str,
        error_code: str,
        *,
        preserve_legacy_queued: bool,
    ) -> list[Job]:
        """Fail queued followups unless their exact route remains recoverable."""
        failed: list[Job] = []
        for operation in self._store.queued_control_operations(participant_id):
            if operation.transport is ControlTransport.PROVIDER_TERMINAL or (
                preserve_legacy_queued and operation.transport is ControlTransport.LEGACY_TMUX
            ):
                continue
            self._store.settle_control_operation(
                operation.operation_id,
                result=DeliveryResult.REJECTED,
                error_code=error_code,
                error="the Theater daemon restarted before this followup was "
                "delivered; it is never replayed automatically",
                updated_at=self._clock(),
            )
            if operation.job_handle:
                job = self._jobs.finish(
                    operation.job_handle,
                    state=JobState.CRASHED,
                    result=(
                        "Queued followup failed: the Theater daemon restarted "
                        "before it was delivered, and undelivered followups are "
                        "never replayed. Queue the prompt again if the work is "
                        "still wanted."
                    ),
                    error_code=error_code,
                )
                if job is not None:
                    failed.append(job)
        return failed

    def _reconcile_running_jobs_at_restart(self, participant_id: str, error_code: str) -> list[Job]:
        """Close the two remaining crash windows around running jobs."""
        failed: list[Job] = []
        for job in self._store.running_jobs_for_target(participant_id):
            operations = self._store.control_operations_for_job(job.handle)
            if not operations:
                if self.route_for(
                    participant_id, RuntimeCapability.SEND
                ).is_native and job.kind in (
                    "send",
                    "spawn",
                ):
                    orphan = self._jobs.finish(
                        job.handle,
                        state=JobState.CRASHED,
                        result=_UNDELIVERED_RESTART_RESULT,
                        error_code=error_code,
                    )
                    if orphan is not None:
                        failed.append(orphan)
                continue  # a legacy job with no operation: the observer owns it
            for op in operations:
                if (
                    op.kind in (ControlKind.SEND, ControlKind.QUEUE_FOLLOWUP)
                    and op.delivery_phase is ControlDeliveryPhase.SETTLED
                    and op.delivery_result is DeliveryResult.REJECTED
                ):
                    # Crash after the operation settled, before the job
                    # finish: close it from the stored refusal facts.
                    settled_job = self._jobs.finish(
                        job.handle,
                        state=JobState.CRASHED,
                        result=op.error or "the native backend refused the prompt",
                        error_code=op.error_code or SEND_REJECTED_ERROR_CODE,
                    )
                    if settled_job is not None:
                        failed.append(settled_job)
                    break
        return failed

    async def reconcile_ambiguous_delivery(
        self, participant_id: str, *, now_ts: float
    ) -> list[Job]:
        """Reconcile uncertain prompt execution without ever mutating it."""
        runtime = self._runtime_for(participant_id)
        resolved: list[Job] = []
        async with self._lock(participant_id):
            operations = {
                operation.operation_id: operation
                for operation in (
                    *self._store.execution_barrier_control_operations(participant_id),
                    *self._store.unresolved_prompt_delivery_operations(participant_id),
                )
                if operation.transport is ControlTransport.NATIVE_RUNTIME
            }
            # The commit-before-finish crash window is resolved before the deadline path.
            if operations:
                resolved.extend(self.finish_jobs_from_pending_evidence([participant_id]))
                operations = {
                    operation.operation_id: operation
                    for operation in (
                        *self._store.execution_barrier_control_operations(participant_id),
                        *self._store.unresolved_prompt_delivery_operations(participant_id),
                    )
                    if operation.transport is ControlTransport.NATIVE_RUNTIME
                }
            snapshot: RuntimeSnapshot | None = None
            if runtime is not None and operations:
                try:
                    snapshot = await runtime.snapshot()
                    self._gates.record_native_snapshot(participant_id, runtime, snapshot)
                except Exception as exc:
                    # A failed state read is UNKNOWN, never idle.
                    logger.warning(
                        "could not snapshot %s during control reconciliation: %s",
                        participant_id,
                        exc,
                    )
            for operation in operations.values():
                resolved.extend(
                    self._reconcile_one_delivery(
                        participant_id, operation, snapshot=snapshot, now_ts=now_ts
                    )
                )
        return resolved

    def _reconcile_one_delivery(
        self,
        participant_id: str,
        operation: ControlOperation,
        *,
        snapshot: RuntimeSnapshot | None,
        now_ts: float,
    ) -> list[Job]:
        """Resolve one ambiguous operation from exact native facts only."""
        evidence = None
        if (
            operation.backend_generation is not None
            and operation.native_session_id is not None
            and operation.native_turn_id is not None
        ):
            evidence = self._store.get_native_terminal_evidence(
                participant_id=participant_id,
                backend_generation=operation.backend_generation,
                native_session_id=operation.native_session_id,
                native_turn_id=operation.native_turn_id,
            )
        if evidence is not None:
            # Committed terminal evidence outranks the snapshot: the turn is over, whatever a stale
            # snapshot still reports.
            self._clear_execution_barrier_for_operation(operation)
            if operation.job_handle is None:
                return []  # a jobless operation carries no recovery obligation
            result = self._finish_from_evidence(participant_id, operation.job_handle, evidence)
            return [] if result is None else [result]
        same_backend_session = (
            snapshot is not None
            and operation.backend_generation is not None
            and operation.native_session_id is not None
            and snapshot.backend_generation == operation.backend_generation
            and snapshot.native_session_id == operation.native_session_id
        )
        barrier_released = False
        if (
            same_backend_session
            and snapshot is not None
            and self._is_authoritatively_idle(snapshot)
        ):
            # This proves the exact execution boundary is clear, not that the prompt completed
            # successfully.
            self._clear_execution_barrier_for_operation(operation, preserve_deadline=True)
            barrier_released = operation.execution_barrier
        if (
            same_backend_session
            and snapshot is not None
            and snapshot.execution_state is RuntimeExecutionState.ACTIVE
            and operation.native_turn_id is not None
            and snapshot.native_turn_id == operation.native_turn_id
        ):
            return []  # exact known active turn: keep waiting for evidence
        # Startup must drain buffered exact evidence before enforcing old delivery deadlines.
        if self._recovering:
            return []
        deadline = max(
            operation.updated_at + AMBIGUOUS_DELIVERY_DEADLINE_SECONDS,
            self._deadline_not_before.get(operation.operation_id, -math.inf),
        )
        if now_ts < deadline:
            return []  # still inside the immediate reconciliation window
        job = self._store.get_job(operation.job_handle) if operation.job_handle else None
        if job is None or job.state != JobState.RUNNING:
            # A terminal job is immutable.  If its barrier was already
            # released by exact idle, its deadline floor is no longer needed.
            if barrier_released or not operation.execution_barrier:
                self._deadline_not_before.pop(operation.operation_id, None)
            return []
        if operation.job_handle is None:
            return []  # a jobless operation carries no recovery obligation
        finished = self._jobs.finish(
            operation.job_handle,
            state=JobState.CRASHED,
            result=(
                "Delivery of this prompt could not be confirmed within "
                f"{AMBIGUOUS_DELIVERY_DEADLINE_SECONDS:.0f}s and the "
                "backend never produced terminal evidence for it. "
                "WARNING: native work may have been accepted and may "
                "still be running; Theater did not resend or fall back "
                "to the pane. Inspect the participant, then re-send if "
                "the work is still wanted."
            ),
            error_code=DELIVERY_UNKNOWN_ERROR_CODE,
        )
        if finished is not None:
            # The deadline closed a job the backend never resolved; the explicit warning above stays
            # the human-facing record.
            self._deadline_not_before.pop(operation.operation_id, None)
            self._count_unknown_delivery(operation.kind, CONTROL_UNKNOWN_DEADLINE)
            return [finished]
        return []
