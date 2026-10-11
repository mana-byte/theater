"""Bounded internal repair of a strict job's invalid final answer.

Only a native-runtime turn has an exact id, so only there can Theater prove which turn answered
the repair prompt. Every other route fails closed to ``unable_to_parse``; callers never see the
attempts, only the single public job.
"""

from __future__ import annotations

import asyncio
import logging
import math

from theater.constants.response_contract import REPAIR_DEADLINE_SECONDS, REPAIR_MAX_ATTEMPTS
from theater.daemon.controls._common import (
    AMBIGUOUS_DELIVERY_DEADLINE_SECONDS,
    CONTROL_UNKNOWN_ACK_LOST,
    INTERRUPTED_ERROR_CODE,
)
from theater.daemon.controls._host import ControlHost
from theater.daemon.controls.dispatch import TEMPORARY_REFUSALS
from theater.daemon.jobs import ContractFailure
from theater.daemon.persistence.repositories.job_repairs import RepairAttempt, RepairState
from theater.daemon.persistence.repositories.native_evidence import NativeTerminalEvidence
from theater.daemon.response_contracts import StrictContract, Verdict, audit, parse_stored
from theater.daemon.response_contracts.prompts import repair_prompt
from theater.harness.contracts.runtime import (
    ControlKind,
    DeliveryResult,
    HarnessRuntime,
    NativeTurnTerminal,
    ResultCompleteness,
    RuntimeCapability,
    RuntimeSnapshot,
)
from theater.models import Job, JobState, Status

logger = logging.getLogger("theater.daemon.controls")

_INTERRUPTED_RESULT = (
    "The native turn was interrupted; the job was cancelled with it. "
    "Re-send the prompt if the work is still wanted."
)


def _attempt_id(job_handle: str, attempt: int) -> str:
    return f"{job_handle}~repair{attempt}"


class RepairControls(ControlHost):
    """Start, send, and settle repair attempts under the participant's control lock."""

    # ---- start ------------------------------------------------------------

    @staticmethod
    def _evaluate(contract: StrictContract, evidence: NativeTerminalEvidence) -> Verdict:
        """A partial read of the answer proves nothing about the whole answer."""
        if evidence.completeness is not ResultCompleteness.COMPLETE:
            return Verdict(False, "incomplete", "Theater could not read your complete final answer")
        return contract.validate(evidence.result)

    def _begin_repair(
        self, participant_id: str, job: Job, evidence: NativeTerminalEvidence, verdict: Verdict
    ) -> Job:
        """Keep ``job`` running while one repair prompt asks its agent to answer again."""
        if self._store.repair_attempts_for_job(job.handle):
            return job  # replayed evidence; the repair already owns this job
        timestamp = self._clock()
        attempt = RepairAttempt(
            attempt_id=_attempt_id(job.handle, 1),
            job_handle=job.handle,
            participant_id=participant_id,
            attempt=1,
            state=RepairState.PENDING,
            backend_generation=evidence.backend_generation,
            native_session_id=evidence.native_session_id,
            source_turn_id=evidence.native_turn_id,
            reason=verdict.detail,
            created_at=timestamp,
            updated_at=timestamp,
        )
        with self._store.write_unit() as unit:
            if not self._store.create_repair_attempt(attempt, connection=unit.connection):
                return job
            audit.record(
                self._store, job, audit.REPAIR_STARTED, verdict.reason, connection=unit.connection
            )
        self._schedule_maintenance(participant_id)
        return job

    # ---- evidence ----------------------------------------------------------

    def _resolve_repair_evidence(
        self, attempt: RepairAttempt, evidence: NativeTerminalEvidence
    ) -> Job | None:
        """Finish or advance the job from the exact terminal evidence of its repair turn."""
        job = self._store.get_job(attempt.job_handle)
        if not attempt.is_open:
            return job  # duplicate or late evidence: the first resolution stands
        if job is None or job.state != JobState.RUNNING:
            self._settle_repair(attempt, "job_finished")
            return job
        if evidence.terminal is NativeTurnTerminal.INTERRUPTED:
            self._cancel_pending_followups(attempt.participant_id, evidence)
            finished = self._jobs.finish(
                job.handle,
                state=JobState.KILLED,
                result=evidence.result or _INTERRUPTED_RESULT,
                error_code=INTERRUPTED_ERROR_CODE,
            )
            audit.record(self._store, job, audit.REPAIR_INTERRUPTED)
            self._settle_repair(attempt, audit.REPAIR_INTERRUPTED)
            return finished
        text = evidence.result
        if attempt.state is RepairState.UNKNOWN or not attempt.turn_proven:
            # The turn is over, but Theater could not prove it answered only the repair prompt.
            event = (
                audit.REPAIR_UNKNOWN
                if attempt.state is RepairState.UNKNOWN
                else (audit.REPAIR_TURN_CONFLICT)
            )
            return self._end_repair(attempt, job, text, event, "the repair turn was not provable")
        if evidence.terminal is NativeTurnTerminal.FAILED:
            return self._end_repair(
                attempt, job, text, audit.REPAIR_TURN_FAILED, evidence.error_code or evidence.error
            )
        contract = parse_stored(job.response_format)
        verdict = (
            self._evaluate(contract, evidence)
            if contract is not None
            else Verdict(False, "contract", "the job no longer carries a strict contract")
        )
        if verdict.ok:
            finished = self._jobs.finish(job.handle, state=JobState.DONE, result=text)
            audit.record(self._store, job, audit.REPAIR_SUCCEEDED, f"attempt {attempt.attempt}")
            self._settle_repair(attempt, audit.REPAIR_SUCCEEDED)
            return finished
        if attempt.attempt < REPAIR_MAX_ATTEMPTS:
            return self._next_attempt(attempt, job, evidence, verdict)
        return self._end_repair(attempt, job, text, audit.REPAIR_EXHAUSTED, verdict.detail)

    def _next_attempt(
        self, attempt: RepairAttempt, job: Job, evidence: NativeTerminalEvidence, verdict: Verdict
    ) -> Job:
        """Close this attempt and open the next in one unit, so the fence never lapses between."""
        timestamp = self._clock()
        follow = RepairAttempt(
            attempt_id=_attempt_id(job.handle, attempt.attempt + 1),
            job_handle=job.handle,
            participant_id=attempt.participant_id,
            attempt=attempt.attempt + 1,
            state=RepairState.PENDING,
            backend_generation=attempt.backend_generation,
            native_session_id=attempt.native_session_id,
            source_turn_id=evidence.native_turn_id,
            reason=verdict.detail,
            created_at=timestamp,
            updated_at=timestamp,
        )
        with self._store.write_unit() as unit:
            self._store.update_repair_attempt(
                attempt.attempt_id,
                updated_at=timestamp,
                state=RepairState.SETTLED,
                fence=False,
                outcome="invalid",
                connection=unit.connection,
            )
            self._store.create_repair_attempt(follow, connection=unit.connection)
            audit.record(
                self._store,
                job,
                audit.REPAIR_STARTED,
                f"attempt {follow.attempt}: {verdict.reason}",
                connection=unit.connection,
            )
        self._schedule_maintenance(attempt.participant_id)
        return job

    def _end_repair(
        self,
        attempt: RepairAttempt,
        job: Job,
        candidate: str | None,
        event: str,
        detail: str | None,
        *,
        release: bool = True,
    ) -> Job | None:
        """The one caller-visible end of a job that never produced a valid answer."""
        finished = self._jobs.finish(
            job.handle,
            state=JobState.DONE,
            result=candidate or "",
            contract_failure=ContractFailure(event, detail),
        )
        self._settle_repair(attempt, event, release=release)
        return finished

    def _settle_repair(self, attempt: RepairAttempt, outcome: str, *, release: bool = True) -> None:
        self._store.update_repair_attempt(
            attempt.attempt_id,
            updated_at=self._clock(),
            state=RepairState.SETTLED,
            fence=not release,
            outcome=outcome,
        )

    def _candidate_text(self, attempt: RepairAttempt) -> str:
        """The latest answer Theater read for this job, for the public ``result`` clip."""
        if attempt.backend_generation is None or attempt.native_session_id is None:
            return ""
        for turn in (attempt.native_turn_id, attempt.source_turn_id):
            if turn is None:
                continue
            evidence = self._store.get_native_terminal_evidence(
                participant_id=attempt.participant_id,
                backend_generation=attempt.backend_generation,
                native_session_id=attempt.native_session_id,
                native_turn_id=turn,
            )
            if evidence is not None and evidence.result:
                return evidence.result
        return ""

    # ---- dispatch ----------------------------------------------------------

    async def maintain_repairs(self, participant_id: str) -> None:
        """One maintenance pass: send what is pending, reconcile what is in flight."""
        async with self._lock(participant_id):
            await self._advance_repairs_locked(participant_id)

    async def _advance_repairs_locked(self, participant_id: str) -> None:
        for attempt in self._store.open_repair_attempts(participant_id):
            if attempt.state is RepairState.PENDING:
                if self._repair_overdue(attempt):
                    self._time_out_unsent(attempt)
                else:
                    await self._dispatch_repair(attempt)
            else:
                self._reconcile_repair(attempt)

    def _repair_overdue(self, attempt: RepairAttempt) -> bool:
        """Past the job's whole-repair deadline, counted from its first attempt in any state."""
        if self._recovering:
            return False  # startup evidence gets its chance before any wall-clock deadline
        first = self._store.repair_attempts_for_job(attempt.job_handle)[0].created_at
        deadline = max(
            first + REPAIR_DEADLINE_SECONDS,
            self._deadline_not_before.get(attempt.attempt_id, -math.inf),
        )
        return self._clock() >= deadline

    def _time_out_unsent(self, attempt: RepairAttempt) -> None:
        """A repair that never reached the wire ends safely: nothing can still be running."""
        job = self._store.get_job(attempt.job_handle)
        if job is not None and job.state == JobState.RUNNING:
            detail = "the repair was never sent within its deadline"
            self._abandon_repair(attempt, job, audit.REPAIR_TIMEOUT, detail)
        else:
            self._settle_repair(attempt, "job_finished")

    def _abandon_repair(
        self, attempt: RepairAttempt, job: Job, event: str, detail: str | None
    ) -> None:
        """Nothing was sent, or it was definitively refused: end the job and release the fence."""
        self._end_repair(attempt, job, self._candidate_text(attempt), event, detail)

    async def _dispatch_repair(self, attempt: RepairAttempt) -> None:
        job = self._store.get_job(attempt.job_handle)
        contract = parse_stored(job.response_format) if job is not None else None
        if job is None or job.state != JobState.RUNNING or contract is None:
            self._settle_repair(attempt, "job_finished")
            return
        if not await self._repair_presence_open(attempt, job):
            return
        ready = await self._repair_ready(attempt, job)
        if ready is None:
            return
        runtime, snapshot = ready
        prompt = repair_prompt(contract, attempt.reason or "it did not match the schema")
        try:
            self._gates.check_prompt(prompt)
        except Exception as exc:
            self._abandon_repair(attempt, job, audit.REPAIR_BLOCKED, str(exc))
            return
        await self._transmit_repair(runtime, attempt, job, prompt, snapshot)

    async def _repair_presence_open(self, attempt: RepairAttempt, job: Job) -> bool:
        """The ordinary presence and preflight gates; a protected participant only defers."""
        participant_id = attempt.participant_id
        try:
            await self._gates.require_absent(participant_id)
            await self._gates.send_preflight(participant_id)
        except TEMPORARY_REFUSALS as exc:
            logger.debug("repair %s deferred: %s", attempt.attempt_id, exc)
            return False
        except Exception as exc:
            # Like queue dispatch: a preflight failure never fails a protected participant.
            try:
                await self._gates.require_absent(participant_id)
            except Exception:
                return False
            self._abandon_repair(attempt, job, audit.REPAIR_BLOCKED, f"preflight: {exc}")
            return False
        return True

    async def _repair_ready(
        self, attempt: RepairAttempt, job: Job
    ) -> tuple[HarnessRuntime, RuntimeSnapshot] | None:
        """The exact runtime and snapshot to send into, or ``None`` after deferring or ending."""
        participant_id = attempt.participant_id
        route = self.route_for(participant_id, RuntimeCapability.SEND)
        if not route.is_native:
            detail = f"the send route is {route.transport}, not an exact native turn"
            self._abandon_repair(attempt, job, audit.UNSUPPORTED_ROUTE, detail)
            return None
        runtime = self._runtime_for(participant_id)
        if runtime is None:
            return None  # disconnected: the fence holds until the exact runtime returns
        try:
            snapshot = await self._snapshot_for_control(runtime, participant_id)
        except TEMPORARY_REFUSALS as exc:
            logger.debug("repair %s deferred: %s", attempt.attempt_id, exc)
            return None
        except Exception as exc:
            logger.info("repair %s could not read its runtime yet: %s", attempt.attempt_id, exc)
            return None
        try:
            route = self._require_current_native_route(
                participant_id, RuntimeCapability.SEND, snapshot, require_available=False
            )
            self._require_capability(participant_id, snapshot, RuntimeCapability.SEND, "send")
        except Exception as exc:
            self._abandon_repair(attempt, job, audit.REPAIR_BLOCKED, str(exc))
            return None
        if not route.route_available:
            return None
        if (snapshot.backend_generation, snapshot.native_session_id) != (
            attempt.backend_generation,
            attempt.native_session_id,
        ):
            detail = "the native session changed before the repair"
            self._abandon_repair(attempt, job, audit.REPAIR_BLOCKED, detail)
            return None
        # Idle is only the precondition to speak; the receipt's turn id is the proof of the answer.
        if not self._is_authoritatively_idle(snapshot):
            return None
        if self._store.has_execution_barrier(participant_id) or any(
            other.handle != job.handle
            for other in self._store.active_running_jobs_for_target(participant_id)
        ):
            return None
        return runtime, snapshot

    async def _transmit_repair(
        self,
        runtime: HarnessRuntime,
        attempt: RepairAttempt,
        job: Job,
        prompt: str,
        snapshot: RuntimeSnapshot,
    ) -> None:
        participant_id = attempt.participant_id
        # DISPATCHED is durable before the wire is touched; the fence stays up throughout.
        self._store.update_repair_attempt(
            attempt.attempt_id, updated_at=self._clock(), state=RepairState.DISPATCHED
        )
        self._schedule_maintenance(participant_id)
        try:
            receipt = await runtime.send(operation_id=attempt.attempt_id, prompt=prompt)
        except asyncio.CancelledError:
            self._mark_repair_unknown(attempt, None)
            raise
        except Exception as exc:
            logger.warning("repair %s delivery is uncertain: %s", attempt.attempt_id, exc)
            self._mark_repair_unknown(attempt, None)
            return
        if receipt.operation_id != attempt.attempt_id:
            self._mark_repair_unknown(attempt, None)
            return
        if receipt.result is DeliveryResult.REJECTED:
            self._abandon_repair(
                attempt, job, audit.REPAIR_REJECTED, receipt.error_code or receipt.error
            )
            return
        if receipt.result is DeliveryResult.ACCEPTED and receipt.native_turn_id is not None:
            self._store.update_repair_attempt(
                attempt.attempt_id,
                updated_at=self._clock(),
                state=RepairState.AWAITING,
                native_turn_id=receipt.native_turn_id,
                turn_proven=not self._turn_is_taken(
                    participant_id, snapshot, receipt.native_turn_id
                ),
            )
            return
        self._mark_repair_unknown(attempt, receipt.native_turn_id)

    def _turn_is_taken(
        self, participant_id: str, snapshot: RuntimeSnapshot, native_turn_id: str
    ) -> bool:
        """A turn another job or attempt already owns cannot prove it answered only the repair."""
        if snapshot.native_session_id is None:
            return True
        if (
            self._operation_for_turn(
                participant_id=participant_id,
                backend_generation=snapshot.backend_generation,
                native_session_id=snapshot.native_session_id,
                native_turn_id=native_turn_id,
            )
            is not None
        ):
            return True
        return (
            self._store.repair_attempt_for_turn(
                participant_id=participant_id,
                backend_generation=snapshot.backend_generation,
                native_session_id=snapshot.native_session_id,
                native_turn_id=native_turn_id,
            )
            is not None
        )

    def _mark_repair_unknown(self, attempt: RepairAttempt, native_turn_id: str | None) -> None:
        """Possibly delivered, never provably ours: never resent, fence held for evidence."""
        self._store.update_repair_attempt(
            attempt.attempt_id,
            updated_at=self._clock(),
            state=RepairState.UNKNOWN,
            native_turn_id=native_turn_id,
            turn_proven=False,
        )
        self._count_unknown_delivery(ControlKind.SEND, CONTROL_UNKNOWN_ACK_LOST)
        self._schedule_maintenance(attempt.participant_id)

    # ---- reconciliation ----------------------------------------------------

    def _repair_evidence(self, attempt: RepairAttempt) -> NativeTerminalEvidence | None:
        if (
            attempt.native_turn_id is None
            or attempt.backend_generation is None
            or attempt.native_session_id is None
        ):
            return None
        return self._store.get_native_terminal_evidence(
            participant_id=attempt.participant_id,
            backend_generation=attempt.backend_generation,
            native_session_id=attempt.native_session_id,
            native_turn_id=attempt.native_turn_id,
        )

    def _repair_context_gone(self, attempt: RepairAttempt) -> bool:
        """Positive evidence the repair turn can no longer be running: its context is gone.

        A missing row or binding proves nothing (a reconnect may be pending); only death or a
        different backend generation or session does.
        """
        participant = self._store.get_participant(attempt.participant_id)
        if participant is not None and participant.status is Status.DEAD:
            return True
        binding = self._store.get_runtime_binding(attempt.participant_id)
        return binding is not None and (binding.backend_generation, binding.native_session_id) != (
            attempt.backend_generation,
            attempt.native_session_id,
        )

    def _reconcile_repair(self, attempt: RepairAttempt) -> None:
        """Resolve an in-flight attempt from exact facts only; idle alone proves nothing."""
        evidence = self._repair_evidence(attempt)
        if evidence is not None:
            self._resolve_repair_evidence(attempt, evidence)
            return
        job = self._store.get_job(attempt.job_handle)
        running = job is not None and job.state == JobState.RUNNING
        if self._repair_context_gone(attempt):
            if job is not None and running:
                self._end_repair(
                    attempt,
                    job,
                    self._candidate_text(attempt),
                    audit.REPAIR_BLOCKED,
                    "the native context ended before the repair answered",
                )
            else:
                self._settle_repair(attempt, "context_gone")
            return
        if job is None or not running or self._recovering:
            return
        if attempt.state in (RepairState.DISPATCHED, RepairState.UNKNOWN):
            ambiguous = max(
                attempt.updated_at + AMBIGUOUS_DELIVERY_DEADLINE_SECONDS,
                self._deadline_not_before.get(attempt.attempt_id, -math.inf),
            )
            if self._clock() < ambiguous and not self._repair_overdue(attempt):
                return
            event, detail = audit.REPAIR_UNKNOWN, "the repair delivery was never confirmed"
        elif attempt.state is RepairState.AWAITING and self._repair_overdue(attempt):
            event, detail = audit.REPAIR_TIMEOUT, "the repair turn did not finish in its deadline"
        else:
            return
        # The caller is not kept waiting on a turn that may still run; the attempt stays open, so
        # the fence holds until that turn's evidence arrives or its context is gone.
        self._jobs.finish(
            job.handle,
            state=JobState.DONE,
            result=self._candidate_text(attempt),
            contract_failure=ContractFailure(event, detail),
        )
        if attempt.state is RepairState.DISPATCHED:
            self._store.update_repair_attempt(
                attempt.attempt_id, updated_at=self._clock(), state=RepairState.UNKNOWN
            )

    def settle_repairs_at_restart(self, participant_ids: list[str]) -> None:
        """A dispatched repair may have reached the wire before the restart; never resend it."""
        for participant_id in participant_ids:
            for attempt in self._store.open_repair_attempts(participant_id):
                if attempt.state is RepairState.DISPATCHED:
                    self._store.update_repair_attempt(
                        attempt.attempt_id, updated_at=self._clock(), state=RepairState.UNKNOWN
                    )

    def resolve_repairs_from_pending_evidence(self, participant_ids: list[str]) -> None:
        """Close the crash window between a repair turn's evidence commit and its job finish."""
        for participant_id in participant_ids:
            for attempt in self._store.open_repair_attempts(participant_id):
                evidence = self._repair_evidence(attempt)
                if evidence is not None:
                    self._resolve_repair_evidence(attempt, evidence)

    # ---- interruption ------------------------------------------------------

    def _cancel_pending_repairs(self, participant_id: str) -> None:
        """An interrupt cancels a repair that was never sent, like any undelivered followup."""
        for attempt in self._store.open_repair_attempts(participant_id):
            if attempt.state is not RepairState.PENDING:
                continue
            job = self._store.get_job(attempt.job_handle)
            if job is not None and job.state == JobState.RUNNING:
                self._jobs.finish(
                    job.handle,
                    state=JobState.KILLED,
                    result=_INTERRUPTED_RESULT,
                    error_code=INTERRUPTED_ERROR_CODE,
                )
                audit.record(self._store, job, audit.REPAIR_INTERRUPTED)
            self._settle_repair(attempt, audit.REPAIR_INTERRUPTED)
