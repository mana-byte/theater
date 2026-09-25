"""Terminal evidence: completing jobs from native turn outcomes."""

from __future__ import annotations

import logging

from theater.daemon.controls._common import INTERRUPTED_ERROR_CODE
from theater.daemon.controls._host import ControlHost
from theater.daemon.persistence.repositories.native_evidence import NativeTerminalEvidence
from theater.harness.contracts.runtime import (
    ControlTransport,
    NativeTurnOutcome,
    NativeTurnTerminal,
)
from theater.models import Job, JobState

logger = logging.getLogger("theater.daemon.controls")

_JOB_STATE_FOR_TERMINAL = {
    NativeTurnTerminal.COMPLETED: JobState.DONE,
    NativeTurnTerminal.FAILED: JobState.CRASHED,
    NativeTurnTerminal.INTERRUPTED: JobState.KILLED,
}


class EvidenceControls(ControlHost):
    """Finish jobs from native terminal evidence."""

    async def record_terminal_evidence(
        self,
        participant_id: str,
        *,
        backend_generation: int,
        outcome: NativeTurnOutcome,
    ) -> Job | None:
        """Persist and process evidence under its participant's control lock."""
        async with self._lock(participant_id):
            return self._record_terminal_evidence_locked(
                participant_id, backend_generation=backend_generation, outcome=outcome
            )

    def _record_terminal_evidence_locked(
        self,
        participant_id: str,
        *,
        backend_generation: int,
        outcome: NativeTurnOutcome,
    ) -> Job | None:
        """Persist terminal evidence, then finish exactly its mapped job."""
        evidence = NativeTerminalEvidence(
            participant_id=participant_id,
            backend_generation=backend_generation,
            native_session_id=outcome.native_session_id,
            native_turn_id=outcome.native_turn_id,
            terminal=outcome.terminal,
            result=outcome.result,
            completeness=outcome.completeness,
            provenance=outcome.provenance,
            error_code=outcome.error_code,
            error=outcome.error,
            recorded_at=self._clock(),
            from_history=outcome.from_history,
            completed_at=outcome.completed_at,
        )
        first_write = self._store.record_native_terminal_evidence(evidence)
        if not first_write:
            # Stored evidence already exists for this exact turn: it wins.
            persisted = self._store.get_native_terminal_evidence(
                participant_id=participant_id,
                backend_generation=backend_generation,
                native_session_id=outcome.native_session_id,
                native_turn_id=outcome.native_turn_id,
            )
            if persisted is not None:
                evidence = persisted
                logger.warning(
                    "duplicate terminal evidence for %s turn %s conflicts with the "
                    "persisted first write; finishing from the persisted evidence",
                    participant_id,
                    outcome.native_turn_id,
                )
        operation = self._operation_for_turn(
            participant_id=participant_id,
            backend_generation=backend_generation,
            native_session_id=outcome.native_session_id,
            native_turn_id=outcome.native_turn_id,
        )
        job: Job | None = None
        current = (
            self._store.get_job(operation.job_handle)
            if operation is not None and operation.job_handle is not None
            else None
        )
        first_processing = current is not None and current.state == JobState.RUNNING
        if evidence.terminal is NativeTurnTerminal.INTERRUPTED and (
            first_write or first_processing
        ):
            # Do this before finishing the active job: an exception at that recoverable boundary
            # must not make a later retry miss queue cancellation.
            self._cancel_pending_followups(participant_id, evidence)
        if operation is not None and operation.job_handle is not None:
            self._clear_execution_barrier_for_operation(operation)
            job = self._finish_from_evidence(participant_id, operation.job_handle, evidence)
        # Historical interruptions can leave unrelated newer followups. They
        # need the same automatic progress opportunity as normal completions.
        self.schedule_dispatch(participant_id)
        self._schedule_maintenance(participant_id)
        return job

    def _finish_from_evidence(
        self, participant_id: str, job_handle: str, evidence: NativeTerminalEvidence
    ) -> Job | None:
        """Finish one job from persisted terminal evidence; exactly once."""
        job = self._store.get_job(job_handle)
        if job is None:
            logger.warning(
                "terminal evidence for %s turn %s maps to missing job %s",
                participant_id,
                evidence.native_turn_id,
                job_handle,
            )
            return None
        if job.state != JobState.RUNNING:
            # Repeated or delayed evidence for an already-terminal job never
            # rewrites its terminal state; the first write stands.
            return job
        state = _JOB_STATE_FOR_TERMINAL[evidence.terminal]
        result = evidence.result
        if evidence.terminal is NativeTurnTerminal.INTERRUPTED:
            result = result or (
                "The native turn was interrupted; the job was cancelled with it. "
                "Re-send the prompt if the work is still wanted."
            )
        error_code = (
            evidence.error_code
            if evidence.error_code is not None
            else (
                INTERRUPTED_ERROR_CODE
                if evidence.terminal is NativeTurnTerminal.INTERRUPTED
                else None
            )
        )
        return self._jobs.finish(job_handle, state=state, result=result, error_code=error_code)

    def finish_jobs_from_pending_evidence(self, participant_ids: list[str]) -> list[Job]:
        """Close the crash window between the evidence commit and job finish."""
        finished: list[Job] = []
        for participant_id in participant_ids:
            for job in self._store.active_running_jobs_for_target(participant_id):
                for operation in self._store.control_operations_for_job(job.handle):
                    if operation.transport is not ControlTransport.NATIVE_RUNTIME:
                        continue
                    if (
                        operation.backend_generation is None
                        or operation.native_session_id is None
                        or operation.native_turn_id is None
                    ):
                        continue
                    evidence = self._store.get_native_terminal_evidence(
                        participant_id=participant_id,
                        backend_generation=operation.backend_generation,
                        native_session_id=operation.native_session_id,
                        native_turn_id=operation.native_turn_id,
                    )
                    if evidence is None:
                        continue
                    if evidence.terminal is NativeTurnTerminal.INTERRUPTED:
                        self._cancel_pending_followups(participant_id, evidence)
                    self._clear_execution_barrier_for_operation(operation)
                    result = self._finish_from_evidence(participant_id, job.handle, evidence)
                    if result is not None and result.state != JobState.RUNNING:
                        finished.append(result)
                        break
        return finished
