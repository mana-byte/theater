"""The harness-neutral daemon control service: the durable control state machine.

One service instance owns every Theater-originated control for all
participants: ordinary send, steering, queued followups, settings updates, and
interruption. Physical facts (pane ownership, human presence, allowlists,
legacy delivery) arrive through :class:`ControlGates`; native delivery goes
through one injected :class:`~theater.harness.contracts.runtime.HarnessRuntime`
per participant. The service is harness-neutral: nothing here imports a
harness plugin, and the fake runtime in ``tests/rig/fake_runtime.py`` is a
faithful stand-in.

State machine (frozen Wave 1 vocabulary):

* Every control is reserved durably before transmission — a
  ``ControlOperation`` row in ``RESERVED`` (or ``QUEUED`` for a followup,
  with its position from the persisted send-sequence allocator).
* ``DISPATCHED`` is persisted *before* transmission begins, so an
  interrupted transmission stays potentially delivered. No operation is ever
  retried and nothing ever falls back to tmux.
* ``SETTLED`` records the terminal delivery result. Job state stays
  ``running``/``done``/``crashed``/``killed`` and is never implied by a
  delivery phase; a job finishes only from exact native terminal evidence.

Accepted limitation, documented and tested: idle checks are guarded, not
atomic against simultaneous native-UI input. Theater controls are serialized
per participant (never a global lock), known-busy targets are rejected, and
when the backend absorbs a Theater send into a UI-started turn the runtime
reports the *actual* returned turn, which is recorded — never fabricated. Two
Theater jobs never bind to one native turn; a conflicting binding fails closed.

Locking: exactly one ``asyncio.Lock`` per participant. A lock is held across
runtime I/O for that participant only — participant B's controls proceed
while participant A's runtime call blocks. Nothing global is ever locked.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass

from theater.constants.daemon import CONTROL_QUEUE_MAX_PENDING
from theater.daemon.controls.gates import ControlGates
from theater.daemon.jobs import JobManager
from theater.daemon.persistence.repositories.control_operations import (
    ControlOperation,
    ControlOperationAmbiguityError,
)
from theater.daemon.persistence.repositories.native_evidence import NativeTerminalEvidence
from theater.daemon.persistence.store import Store
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlReceipt,
    ControlTransport,
    DeliveryResult,
    HarnessRuntime,
    NativeTurnOutcome,
    NativeTurnTerminal,
    RuntimeCapability,
    RuntimeSnapshot,
    RuntimeWiring,
)
from theater.models import (
    AwaitingDecision,
    BadRequest,
    Busy,
    HumanPresent,
    Job,
    JobState,
    StaleTarget,
    now,
)

__all__ = [
    "AMBIGUOUS_DELIVERY_DEADLINE_SECONDS",
    "ControlService",
    "InterruptOutcome",
    "QueueDispatchOutcome",
    "SettingsOutcome",
]

logger = logging.getLogger("theater.daemon.controls")

#: Seconds an ambiguously delivered operation may stay unresolved before its
#: job finishes ``crashed`` with ``delivery_unknown`` — the plan's 30-second
#: immediate ambiguous-delivery reconciliation deadline.
AMBIGUOUS_DELIVERY_DEADLINE_SECONDS = 30.0

DAEMON_RESTARTED_ERROR_CODE = "daemon_restarted"
DELIVERY_UNKNOWN_ERROR_CODE = "delivery_unknown"
INTERRUPTED_ERROR_CODE = "interrupted"
NATIVE_TURN_CONFLICT_ERROR_CODE = "native_turn_conflict"
SEND_REJECTED_ERROR_CODE = "send_rejected"

#: The job result for a prompt that never began transmission before the
#: daemon restart: failed, never replayed, and safe to send again.
_UNDELIVERED_RESTART_RESULT = (
    "Send failed: the Theater daemon restarted before the prompt was "
    "transmitted, and an undelivered prompt is never replayed. Send it "
    "again if the work is still wanted."
)

#: Refusals that are temporary: a queued followup hit by one stays queued.
TEMPORARY_REFUSALS = (Busy, HumanPresent, AwaitingDecision)

_JOB_STATE_FOR_TERMINAL = {
    NativeTurnTerminal.COMPLETED: JobState.DONE,
    NativeTurnTerminal.FAILED: JobState.CRASHED,
    NativeTurnTerminal.INTERRUPTED: JobState.KILLED,
}

#: The actions the authorize gate is asked about.
ACTION_SEND = "send"
ACTION_STEER = "steer"
ACTION_QUEUE_FOLLOWUP = "queue_followup"
ACTION_QUEUE_DISPATCH = "queue_dispatch"
ACTION_SETTINGS_UPDATE = "settings_update"
ACTION_INTERRUPT = "interrupt"


@dataclass(frozen=True, slots=True)
class SettingsOutcome:
    """What one settings update established.

    ``applied`` is ``True`` only after native confirmation/readback, ``False``
    on a definitive refusal, and ``None`` when delivery was uncertain —
    uncertain application stays visibly uncertain; effective values are
    reported only after confirmation.
    """

    applied: bool | None
    model: str | None = None
    reasoning_effort: str | None = None
    error_code: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class InterruptOutcome:
    """What one interrupt did."""

    #: Whether a native interruption was requested and accepted.
    interrupted: bool
    #: ``already_idle`` when there was no active turn to interrupt.
    reason: str | None = None
    #: Job handles cancelled out of the queue before they were ever delivered.
    cancelled_followups: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class QueueDispatchOutcome:
    """One dispatch pass over a participant's followup queue."""

    #: Job handles dispatched this pass (at most one — prompts go one at a
    #: time; later passes are triggered by terminal evidence).
    dispatched: tuple[str, ...] = ()
    #: ``(job_handle, error_code)`` for items that failed definitively.
    failed: tuple[tuple[str, str], ...] = ()
    #: True when the queue head stayed queued on a temporary condition.
    deferred: bool = False


class ControlService:
    """Durable control state machine for every Theater-originated control."""

    def __init__(
        self,
        *,
        store: Store,
        jobs: JobManager,
        runtime_for: Callable[[str], HarnessRuntime | None],
        gates: ControlGates,
    ) -> None:
        #: ``None`` means no live runtime. A participant with no persisted
        #: native binding is legacy; a persisted native binding without a
        #: live runtime is a disconnected native whose controls fail closed.
        self._runtime_for = runtime_for
        self._store = store
        self._jobs = jobs
        self._gates = gates
        self._locks: dict[str, asyncio.Lock] = {}
        self._dispatch_tasks: dict[str, asyncio.Task[QueueDispatchOutcome]] = {}

    # ---- ordinary send ---------------------------------------------------

    async def send(
        self,
        participant_id: str,
        *,
        caller_id: str,
        prompt: str,
        response_format: str | None = None,
        job_handle: str | None = None,
    ) -> Job:
        """Ordinary send — or the native initial dispatch of one spawn job.

        Retains the current pane-ownership, addressability, copy-mode
        human-presence, and policy preflights through the injected gates,
        adds the authoritative runtime-snapshot idle check, refuses
        known-busy targets, and never jumps ahead of a queued followup. The
        durable operation is reserved before transmission either way.

        ``job_handle`` is the additive native-lifecycle seam: when supplied,
        it must name the existing ``RUNNING`` spawn job of exactly this
        target/caller/prompt contract; the SEND operation is then reserved
        against that job and dispatched once — no second job is created,
        and the job's terminal evidence finishes it. A wrong, terminal, or
        vanished handle fails closed before transmission. Callers that omit
        it keep the exact ordinary-send behavior.
        """
        runtime = self._runtime_for(participant_id)
        async with self._lock(participant_id):
            self._gates.authorize(participant_id, caller_id, ACTION_SEND)
            self._gates.check_prompt(prompt)
            await self._gates.send_preflight(participant_id)
            if runtime is None:
                if self._participant_is_native(participant_id):
                    # A persisted native binding without a live runtime is a
                    # disconnected native participant, not a legacy one.
                    raise self._disconnected_native_refusal(participant_id, "send")
                if job_handle is not None:
                    raise BadRequest(
                        f"reusing job {job_handle!r} for the initial dispatch of "
                        f"participant {participant_id!r} requires native runtime "
                        "wiring; its harness has no runtime, so the prompt can only "
                        "be sent as an ordinary send"
                    )
                return await self._send_legacy(
                    participant_id,
                    caller_id=caller_id,
                    prompt=prompt,
                    response_format=response_format,
                )
            # The reused spawn job is validated before any runtime I/O: a
            # wrong handle fails closed with nothing sent and nothing minted.
            job: Job | None = None
            if job_handle is not None:
                job = self._reusable_spawn_job(
                    participant_id,
                    job_handle=job_handle,
                    caller_id=caller_id,
                    prompt=prompt,
                    response_format=response_format,
                )
            snapshot = await runtime.snapshot()
            self._require_capability(participant_id, snapshot, RuntimeCapability.SEND, "send")
            self._reject_busy(
                participant_id,
                snapshot,
                exclude=job.handle if job is not None else None,
            )
            # No await from here through reservation: the idle check and the
            # reservation are one guarded step. The remaining race — a
            # simultaneous native-UI submission absorbing this prompt into a
            # UI-started turn — is documented and accepted; the runtime
            # reports the actual returned turn and it is recorded as-is.
            if job is None:
                job = self._create_send_job(
                    participant_id,
                    caller_id=caller_id,
                    prompt=prompt,
                    response_format=response_format,
                )
            operation_id = self._mint_operation_id(participant_id, ControlKind.SEND)
            self._reserve(
                operation_id,
                participant_id=participant_id,
                kind=ControlKind.SEND,
                transport=ControlTransport.NATIVE_RUNTIME,
                phase=ControlDeliveryPhase.RESERVED,
                job_handle=job.handle,
                backend_generation=snapshot.backend_generation,
                native_session_id=snapshot.native_session_id,
            )
            await self._deliver_native(
                runtime,
                participant_id=participant_id,
                operation_id=operation_id,
                prompt=prompt,
                job_handle=job.handle,
                snapshot=snapshot,
            )
            return self._require_job(job.handle)

    async def _send_legacy(
        self,
        participant_id: str,
        *,
        caller_id: str,
        prompt: str,
        response_format: str | None,
    ) -> Job:
        """Legacy transport: the same durable receipt transitions, no runtime.

        The injected gates carry the existing legacy busy semantics and the
        tmux delivery; ``deliver_text`` raising means nothing was delivered,
        so the job closes immediately, exactly like the existing send path.
        """
        await self._gates.legacy_busy_check(participant_id)
        job = self._create_send_job(
            participant_id,
            caller_id=caller_id,
            prompt=prompt,
            response_format=response_format,
        )
        operation_id = self._mint_operation_id(participant_id, ControlKind.SEND)
        self._reserve(
            operation_id,
            participant_id=participant_id,
            kind=ControlKind.SEND,
            transport=ControlTransport.LEGACY_TMUX,
            phase=ControlDeliveryPhase.RESERVED,
            job_handle=job.handle,
        )
        self._store.mark_control_operation_dispatched(operation_id, updated_at=self._clock())
        try:
            await self._gates.legacy_deliver(participant_id, prompt)
        except Exception as exc:
            # Nothing was delivered, so nothing will ever answer.
            self._store.settle_control_operation(
                operation_id,
                result=DeliveryResult.REJECTED,
                error_code="send_failed",
                error=str(exc),
                updated_at=self._clock(),
            )
            self._jobs.finish(
                job.handle,
                state=JobState.CRASHED,
                result=str(exc),
                error_code="send_failed",
            )
            raise
        self._store.settle_control_operation(
            operation_id, result=DeliveryResult.ACCEPTED, updated_at=self._clock()
        )
        return self._require_job(job.handle)

    # ---- steering --------------------------------------------------------

    async def steer(
        self,
        participant_id: str,
        *,
        caller_id: str,
        prompt: str,
        job_handle: str | None = None,
    ) -> Job:
        """Amend exactly the current Theater job's active native turn.

        Requires an active native turn mapped to a running Theater job and
        sends its exact expected turn id. The amendment is stored against
        that job; the original prompt and response-format contract are
        preserved, no new job handle is created, and a stale-turn or
        no-active-turn refusal stays a refusal — never reinterpreted as a
        send or a queue entry. Authorization runs before any participant
        state is revealed: an unauthorized caller learns nothing about the
        wiring, and a disconnected native participant fails closed with no
        mutation.
        """
        runtime = self._runtime_for(participant_id)
        async with self._lock(participant_id):
            self._gates.authorize(participant_id, caller_id, ACTION_STEER)
            self._gates.check_prompt(prompt)
            if runtime is None:
                if self._participant_is_native(participant_id):
                    raise self._disconnected_native_refusal(participant_id, "steer")
                raise BadRequest(
                    f"steering participant {participant_id!r} requires native runtime "
                    "wiring; its harness has no runtime, so the prompt can only be "
                    "sent with the ordinary idle-guarded send (wait for "
                    "status='idle') or queued as a followup"
                )
            snapshot = await runtime.snapshot()
            self._require_capability(participant_id, snapshot, RuntimeCapability.STEER, "steering")
            expected_turn = snapshot.native_turn_id
            if expected_turn is None:
                raise StaleTarget(
                    f"participant {participant_id!r} has no active native turn to "
                    "steer; wait for status='idle' and use send, or queue a followup"
                )
            operation = self._operation_for_snapshot_turn(participant_id, snapshot)
            if operation is None or operation.job_handle is None:
                raise StaleTarget(
                    f"the active native turn of participant {participant_id!r} belongs "
                    "to no Theater job (a human started it in the native UI); steering "
                    "refuses instead of creating a synthetic job"
                )
            if job_handle is not None and operation.job_handle != job_handle:
                raise StaleTarget(
                    f"the active native turn of participant {participant_id!r} maps to "
                    f"job {operation.job_handle!r}, not {job_handle!r}; refusing to "
                    "amend a different job than the one you expect"
                )
            job = self._require_job(operation.job_handle)
            if job.state != JobState.RUNNING:
                raise StaleTarget(
                    f"the active native turn of participant {participant_id!r} maps to "
                    f"job {job.handle!r}, which is already {job.state}; nothing to amend"
                )
            operation_id = self._mint_operation_id(participant_id, ControlKind.STEER)
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
                payload=prompt,
            )
            self._store.mark_control_operation_dispatched(
                operation_id,
                native_session_id=snapshot.native_session_id,
                native_turn_id=expected_turn,
                updated_at=self._clock(),
            )
            try:
                receipt = await runtime.steer(
                    operation_id=operation_id, native_turn_id=expected_turn, prompt=prompt
                )
            except Exception as exc:
                # Transmission was uncertain; a steer carries no completion
                # obligation of its own (the send operation's terminal
                # evidence finishes the job), so record the uncertainty and
                # let the job run. Never retry, never fall back.
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
                return job
            if not self._receipt_names_operation(operation_id, receipt):
                # The amendment cannot be confirmed for this operation; the
                # steer stays uncertain and is never retried. The job runs
                # on — its send operation's terminal evidence finishes it.
                self._settle_uncertain(
                    operation_id,
                    error=(
                        f"the steering receipt named operation {receipt.operation_id!r}, "
                        f"not {operation_id!r}; the amendment is uncertain and the "
                        "receipt is not trusted to settle it"
                    ),
                )
                return job
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
            return job

    # ---- queued followups -------------------------------------------------

    async def queue_followup(
        self,
        participant_id: str,
        *,
        caller_id: str,
        prompt: str,
        response_format: str | None = None,
    ) -> Job:
        """Create an awaitable send job immediately and reserve its queue slot.

        The queue lives entirely in Theater. The position comes from the
        persisted send-sequence allocator, allocated in one transaction with
        the ``QUEUED`` reservation; the bound is enforced before creation.
        The job is created without a path accumulator so a pending followup
        can never receive path touches; the accumulator is attached when the
        item dispatches and the job becomes active. A disconnected native
        participant (persisted native binding, no live runtime) refuses
        before any reservation: its followups are never queued as legacy
        work, and nothing is reserved or created.
        """
        async with self._lock(participant_id):
            self._gates.authorize(participant_id, caller_id, ACTION_QUEUE_FOLLOWUP)
            self._gates.check_prompt(prompt)
            pending = self._store.queued_control_operation_count(participant_id)
            if pending >= CONTROL_QUEUE_MAX_PENDING:
                raise Busy(
                    f"participant {participant_id!r} already holds {pending} queued "
                    f"followups (bound {CONTROL_QUEUE_MAX_PENDING}); await or "
                    "interrupt the pending handles before queueing another"
                )
            # The queue slot is generation-guarded: the exact-turn mapping
            # needs participant + generation + session + turn, and the
            # reservation is the only write that can carry the generation.
            # A backend relaunch before dispatch fails the item instead of
            # replaying it — never across generations.
            runtime = self._runtime_for(participant_id)
            generation: int | None = None
            session: str | None = None
            if runtime is not None:
                snapshot = await runtime.snapshot()
                # The queue is Theater-owned, so the QUEUE_FOLLOWUP capability
                # (forbidden native thread/queue use) never gates it. Native
                # delivery needs SEND, checked here and again at dispatch
                # because capabilities may change.
                self._require_capability(participant_id, snapshot, RuntimeCapability.SEND, "send")
                generation = snapshot.backend_generation
                session = snapshot.native_session_id
            elif self._participant_is_native(participant_id):
                raise self._disconnected_native_refusal(participant_id, "queue_followup")
            with self._store.runtime_transaction() as connection:
                sequence = self._store.allocate_control_queue_sequence(connection=connection)
                handle = f"{participant_id}#{sequence}"
                self._reserve(
                    f"{handle}:{ControlKind.QUEUE_FOLLOWUP.value}",
                    participant_id=participant_id,
                    kind=ControlKind.QUEUE_FOLLOWUP,
                    transport=self._transport_for(participant_id),
                    phase=ControlDeliveryPhase.QUEUED,
                    job_handle=handle,
                    backend_generation=generation,
                    native_session_id=session,
                    queue_sequence=sequence,
                    connection=connection,
                )
            self._jobs.create(
                handle=handle,
                caller_id=caller_id,
                target_id=participant_id,
                kind="send",
                prompt=prompt,
                cwd=None,
                response_format=response_format,
            )
            job = self._require_job(handle)
        # Already idle? Dispatch on the next scheduling opportunity.
        self.schedule_dispatch(participant_id)
        return job

    def schedule_dispatch(self, participant_id: str) -> None:
        """Try to dispatch the queue head on the next scheduling opportunity.

        A no-op outside a running event loop (a synchronous caller such as
        restart reconciliation has nothing to dispatch — restart fails
        undelivered followups, it never replays them).
        """
        existing = self._dispatch_tasks.get(participant_id)
        if existing is not None and not existing.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._dispatch_pass_logged(participant_id))
        self._dispatch_tasks[participant_id] = task

    async def _dispatch_pass_logged(self, participant_id: str) -> QueueDispatchOutcome:
        """Run one dispatch pass so its crash is logged, never unretrieved.

        A pass that raises would otherwise leave an unretrieved task
        exception behind. There is no blind retry: the crashed pass is over
        and whatever it touched stays queued. The task completes normally,
        so a later scheduling opportunity sees no live task and runs
        another pass over whatever is still queued.
        """
        try:
            return await self.dispatch_queue(participant_id)
        except Exception:
            logger.exception(
                "queue dispatch pass for %s crashed; no retry — a later "
                "scheduling opportunity will run another pass",
                participant_id,
            )
            return QueueDispatchOutcome()

    async def dispatch_queue(self, participant_id: str) -> QueueDispatchOutcome:
        """Dispatch queue items one at a time, after an authoritative idle check.

        Ownership and policy are revalidated at dispatch. A temporary
        condition (busy, human present, pending native interaction) leaves
        the item queued; a definitive one (lost ownership, dead target,
        definitive refusal) finishes that item with an explicit error and
        lets the queue continue with the next.
        """
        dispatched: list[str] = []
        failed: list[tuple[str, str]] = []
        deferred = False
        async with self._lock(participant_id):
            while True:
                outcome = await self._dispatch_head(participant_id)
                dispatched.extend(outcome.dispatched)
                failed.extend(outcome.failed)
                if outcome.deferred:
                    deferred = True
                if not outcome.dispatched and not outcome.failed:
                    break
                # A definitive failure removed one item; try the next. A
                # successful dispatch stops here — prompts go one at a time
                # and the next pass waits for terminal evidence.
                if outcome.dispatched:
                    break
        return QueueDispatchOutcome(
            dispatched=tuple(dispatched), failed=tuple(failed), deferred=deferred
        )

    async def _dispatch_head(self, participant_id: str) -> QueueDispatchOutcome:
        queued = self._store.queued_control_operations(participant_id)
        if not queued:
            return QueueDispatchOutcome()
        head = queued[0]
        job = self._store.get_job(head.job_handle) if head.job_handle else None
        if job is None or job.state != JobState.RUNNING:
            # The job vanished before dispatch (crash residue); the queue
            # slot is definitively unanswerable.
            self._store.settle_control_operation(
                head.operation_id,
                result=DeliveryResult.REJECTED,
                error_code="job_missing",
                error=f"queued job {head.job_handle!r} is no longer running",
                updated_at=self._clock(),
            )
            return QueueDispatchOutcome(failed=((head.job_handle or "", "job_missing"),))
        try:
            self._gates.authorize(participant_id, job.caller_id, ACTION_QUEUE_DISPATCH)
        except Exception as exc:
            return self._fail_queued_item(head, job, exc)
        try:
            await self._gates.send_preflight(participant_id)
        except TEMPORARY_REFUSALS as exc:
            logger.debug("queued followup %s deferred: %s", head.operation_id, exc)
            return QueueDispatchOutcome(deferred=True)
        except Exception as exc:
            return self._fail_queued_item(head, job, exc)
        runtime = self._runtime_for(participant_id)
        if runtime is not None:
            return await self._dispatch_head_native(runtime, participant_id, head, job)
        if self._participant_is_native(participant_id):
            # A disconnected native participant: its queued followup is
            # never delivered through the legacy pane. Deferred, not
            # failed — reconnect or restart reconciliation decides its
            # fate, and this pass mutates nothing.
            logger.info(
                "queued followup %s of %s deferred: the natively-wired "
                "participant's runtime is not connected; no legacy pane delivery",
                head.operation_id,
                participant_id,
            )
            return QueueDispatchOutcome(deferred=True)
        try:
            await self._gates.legacy_busy_check(participant_id)
        except TEMPORARY_REFUSALS as exc:
            # A temporarily busy legacy pane defers the head exactly like
            # the native busy path: the item stays queued and unmutated,
            # and a later pass dispatches it once the active work settles.
            logger.debug("queued followup %s deferred: %s", head.operation_id, exc)
            return QueueDispatchOutcome(deferred=True)
        except Exception as exc:
            return self._fail_queued_item(head, job, exc)
        self._store.mark_control_operation_dispatched(head.operation_id, updated_at=self._clock())
        try:
            await self._gates.legacy_deliver(participant_id, job.prompt or "")
        except Exception as exc:
            return self._fail_queued_item(head, job, exc)
        self._store.settle_control_operation(
            head.operation_id,
            result=DeliveryResult.ACCEPTED,
            updated_at=self._clock(),
        )
        return QueueDispatchOutcome(dispatched=(job.handle,))

    async def _dispatch_head_native(
        self,
        runtime: HarnessRuntime,
        participant_id: str,
        head: ControlOperation,
        job: Job,
    ) -> QueueDispatchOutcome:
        snapshot = await runtime.snapshot()
        try:
            # Native delivery needs SEND: the followup queue is
            # Theater-owned, and QUEUE_FOLLOWUP marks forbidden native
            # queue use, so it never gates this path. A SEND capability
            # lost since the reservation is definitive — the item fails
            # with the recorded reason, never retried.
            self._require_capability(participant_id, snapshot, RuntimeCapability.SEND, "send")
        except Exception as exc:
            return self._fail_queued_item(head, job, exc)
        if (
            snapshot.native_turn_id is not None
            or snapshot.pending_interaction is not None
            or self._store.active_running_jobs_for_target(participant_id)
        ):
            return QueueDispatchOutcome(deferred=True)
        if (
            head.backend_generation is not None
            and head.backend_generation != snapshot.backend_generation
        ):
            # The backend relaunched since the reservation; the slot was
            # reserved against a generation that no longer exists, so it
            # fails instead of replaying into the new backend.
            return self._fail_queued_item(
                head,
                job,
                StaleTarget(
                    f"the native backend of {participant_id!r} restarted "
                    f"(reserved generation {head.backend_generation}, now "
                    f"{snapshot.backend_generation}); the queued followup "
                    "is never replayed into the new backend"
                ),
            )
        self._store.mark_control_operation_dispatched(
            head.operation_id,
            native_session_id=snapshot.native_session_id,
            updated_at=self._clock(),
        )
        cwd = self._gates.cwd_for(participant_id)
        if cwd is not None:
            self._jobs.attach_touch_accumulator(job.handle, cwd=cwd)
        await self._deliver_native(
            runtime,
            participant_id=participant_id,
            operation_id=head.operation_id,
            prompt=job.prompt or "",
            job_handle=job.handle,
            snapshot=snapshot,
        )
        return QueueDispatchOutcome(dispatched=(job.handle,))

    def _fail_queued_item(
        self, operation: ControlOperation, job: Job, exc: Exception
    ) -> QueueDispatchOutcome:
        """Finish one queued item with an explicit error; never replay it."""
        error_code = _error_code_of(exc)
        self._store.settle_control_operation(
            operation.operation_id,
            result=DeliveryResult.REJECTED,
            error_code=error_code,
            error=str(exc),
            updated_at=self._clock(),
        )
        self._jobs.finish(
            job.handle,
            state=JobState.CRASHED,
            result=str(exc),
            error_code=error_code,
        )
        logger.info(
            "queued followup %s for job %s failed at dispatch (%s): %s",
            operation.operation_id,
            job.handle,
            error_code,
            exc,
        )
        return QueueDispatchOutcome(failed=((job.handle, error_code),))

    # ---- settings ---------------------------------------------------------

    async def update_settings(
        self,
        participant_id: str,
        *,
        caller_id: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> SettingsOutcome:
        """Idle-only model/reasoning update, capability- and allowlist-gated.

        Only the supplied fields are sent; approval and sandbox policy are
        immutable — this service has no parameter that could carry them.
        Effective values are reported only after native confirmation; an
        uncertain delivery stays visibly uncertain. Authorization runs
        before any participant state is revealed; a disconnected native
        participant fails closed with no mutation.
        """
        if model is None and reasoning_effort is None:
            raise BadRequest(
                f"nothing to update for participant {participant_id!r}: supply "
                "model and/or reasoning_effort; approval and sandbox policy are "
                "never changed here"
            )
        runtime = self._runtime_for(participant_id)
        async with self._lock(participant_id):
            self._gates.authorize(participant_id, caller_id, ACTION_SETTINGS_UPDATE)
            self._gates.check_settings(model, reasoning_effort)
            if runtime is None:
                if self._participant_is_native(participant_id):
                    raise self._disconnected_native_refusal(participant_id, "settings update")
                raise BadRequest(
                    f"settings updates for participant {participant_id!r} require native "
                    "runtime wiring; its harness has no runtime, so the model is "
                    "fixed at launch"
                )
            snapshot = await runtime.snapshot()
            if not snapshot.capabilities.supports(RuntimeCapability.SETTINGS_UPDATE):
                reason = snapshot.capabilities.reason_for(RuntimeCapability.SETTINGS_UPDATE)
                raise BadRequest(
                    f"participant {participant_id!r} does not support settings "
                    f"updates ({reason}); the installed native API gates this "
                    "capability, so the model stays as configured at launch"
                )
            self._reject_busy(participant_id, snapshot, idle_only=True)
            payload = json.dumps(
                {
                    key: value
                    for key, value in (
                        ("model", model),
                        ("reasoning_effort", reasoning_effort),
                    )
                    if value is not None
                }
            )
            operation_id = self._mint_operation_id(participant_id, ControlKind.SETTINGS_UPDATE)
            self._reserve(
                operation_id,
                participant_id=participant_id,
                kind=ControlKind.SETTINGS_UPDATE,
                transport=ControlTransport.NATIVE_RUNTIME,
                phase=ControlDeliveryPhase.RESERVED,
                backend_generation=snapshot.backend_generation,
                native_session_id=snapshot.native_session_id,
                payload=payload,
            )
            self._store.mark_control_operation_dispatched(
                operation_id,
                native_session_id=snapshot.native_session_id,
                updated_at=self._clock(),
            )
            try:
                receipt = await runtime.update_settings(
                    operation_id=operation_id,
                    model=model,
                    reasoning_effort=reasoning_effort,
                )
            except Exception as exc:
                # No Theater job hangs on a settings operation; record the
                # uncertainty so the row is honestly settled and prunable.
                self._store.settle_control_operation(
                    operation_id,
                    result=DeliveryResult.UNKNOWN,
                    error_code=DELIVERY_UNKNOWN_ERROR_CODE,
                    error=str(exc),
                    updated_at=self._clock(),
                )
                logger.warning("settings update for %s is uncertain: %s", participant_id, exc)
                return SettingsOutcome(applied=None, model=model, reasoning_effort=reasoning_effort)
            if not self._receipt_names_operation(operation_id, receipt):
                self._settle_uncertain(
                    operation_id,
                    error=(
                        f"the settings receipt named operation {receipt.operation_id!r}, "
                        f"not {operation_id!r}; the update is uncertain and the "
                        "receipt is not trusted to settle it"
                    ),
                )
                return SettingsOutcome(
                    applied=None,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    error_code=DELIVERY_UNKNOWN_ERROR_CODE,
                    error="the settings update stayed uncertain: the native receipt "
                    "named a different operation",
                )
            self._settle_from_receipt(operation_id, receipt)
            if receipt.result is DeliveryResult.REJECTED:
                return SettingsOutcome(
                    applied=False,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    error_code=receipt.error_code,
                    error=receipt.error,
                )
            if receipt.result is DeliveryResult.UNKNOWN:
                logger.warning("settings update for %s stayed uncertain", participant_id)
                return SettingsOutcome(applied=None, model=model, reasoning_effort=reasoning_effort)
            # Effective values only after native confirmation/readback. The
            # delivery stays an accepted fact even when the readback fails,
            # but the application itself is then unknown — it is reported as
            # explicitly uncertain, never as success and never by raising.
            try:
                fresh = await runtime.snapshot()
            except Exception as exc:
                logger.warning(
                    "settings update for %s was accepted but the effective-value "
                    "readback failed: %s; the application stays uncertain",
                    participant_id,
                    exc,
                )
                return SettingsOutcome(
                    applied=None,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    error_code=DELIVERY_UNKNOWN_ERROR_CODE,
                    error=(
                        "the native backend accepted the settings update, but the "
                        "effective-value readback failed; whether the application "
                        "took effect is unknown"
                    ),
                )
            return SettingsOutcome(
                applied=True,
                model=fresh.settings.model,
                reasoning_effort=fresh.settings.reasoning_effort,
            )

    # ---- interrupt --------------------------------------------------------

    async def interrupt(self, participant_id: str, *, caller_id: str) -> InterruptOutcome:
        """Cancel every undelivered followup, then interrupt the active turn.

        The queue cancellation is durable and happens under the same
        per-participant lock as dispatch, so no cancelled item can cross the
        cancellation boundary and start afterwards. The active job itself is
        finished only later, from authoritative terminal evidence. An
        interruption request while already idle still clears the queue.
        Authorization runs before any participant state is revealed and
        before the queue cancellation; a disconnected native participant
        fails closed with no mutation — its queue is not even touched.
        """
        runtime = self._runtime_for(participant_id)
        async with self._lock(participant_id):
            self._gates.authorize(participant_id, caller_id, ACTION_INTERRUPT)
            if runtime is None:
                if self._participant_is_native(participant_id):
                    raise self._disconnected_native_refusal(participant_id, "interrupt")
                raise BadRequest(
                    f"interrupting participant {participant_id!r} through the control "
                    "service requires native runtime wiring; its harness uses the "
                    "existing pane-interrupt path"
                )
            cancelled = await self._cancel_queued_followups(participant_id)
            snapshot = await runtime.snapshot()
            self._require_capability(
                participant_id, snapshot, RuntimeCapability.INTERRUPT, "interruption"
            )
            turn = snapshot.native_turn_id
            if turn is None:
                return InterruptOutcome(
                    interrupted=False, reason="already_idle", cancelled_followups=cancelled
                )
            operation_id = self._mint_operation_id(participant_id, ControlKind.INTERRUPT)
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
            self._store.mark_control_operation_dispatched(
                operation_id,
                native_session_id=snapshot.native_session_id,
                native_turn_id=turn,
                updated_at=self._clock(),
            )
            try:
                receipt = await runtime.interrupt(operation_id=operation_id, native_turn_id=turn)
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
        """A native-UI-initiated interruption: cancel the pending queue.

        The human already pressed the interrupt in the native UI; Theater
        sends nothing. What Theater must still do is cancel its own undelivered
        followups so the next queued prompt cannot start after the human
        stopped the session.
        """
        del native_turn_id  # the exact turn is already gone; nothing to request
        async with self._lock(participant_id):
            return await self._cancel_queued_followups(participant_id)

    async def _cancel_queued_followups(self, participant_id: str) -> tuple[str, ...]:
        """Durably cancel every queued followup; return the cancelled handles."""
        cancelled: list[str] = []
        for operation in self._store.queued_control_operations(participant_id):
            self._store.settle_control_operation(
                operation.operation_id,
                result=DeliveryResult.REJECTED,
                error_code=INTERRUPTED_ERROR_CODE,
                error="interrupted before dispatch; the active turn was interrupted",
                updated_at=self._clock(),
            )
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

    # ---- terminal evidence and completion ---------------------------------

    async def record_terminal_evidence(
        self,
        participant_id: str,
        *,
        backend_generation: int,
        outcome: NativeTurnOutcome,
    ) -> Job | None:
        """Persist terminal evidence, then finish exactly its mapped job.

        The evidence commit precedes the job finish; a crash between the two
        is the intentional recoverable crash point that
        :meth:`finish_jobs_from_pending_evidence` closes exactly once.
        Completion maps the exact native turn through the control-operation
        lookup — never an oldest-running heuristic. A turn with no
        job-bearing operation (a human turn) completes nothing; an ambiguous
        mapping fails closed and finishes nothing. Evidence is
        first-write-wins: a conflicting duplicate that arrives while the job
        is still running finishes the job from the *persisted* first
        evidence, never from the incoming duplicate. The queue-cancellation
        side effect of an ``INTERRUPTED`` evidence follows the same rule: it
        runs for the first insertion (or the first processing after the
        crash window) and never again — a replay cannot cancel followups
        queued after the first processing.
        """
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
        )
        first_write = self._store.record_native_terminal_evidence(evidence)
        if not first_write:
            # Stored evidence already exists for this exact turn: it wins.
            # Finish from the persisted row, never from the incoming
            # duplicate outcome, so a crash between commit and finish
            # reconciles to the same first terminal facts.
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
        async with self._lock(participant_id):
            operation = self._operation_for_turn(
                participant_id=participant_id,
                backend_generation=backend_generation,
                native_session_id=outcome.native_session_id,
                native_turn_id=outcome.native_turn_id,
            )
            job: Job | None = None
            # Whether this call is the first time the evidence is being
            # processed: first write, or the mapped job was still running
            # (the crash window between the evidence commit and the job
            # finish). A replay whose processing already happened must not
            # repeat the irreversible queue-cancellation side effect.
            first_processing = False
            if operation is not None and operation.job_handle is not None:
                current = self._store.get_job(operation.job_handle)
                first_processing = current is not None and current.state == JobState.RUNNING
                job = self._finish_from_evidence(participant_id, operation.job_handle, evidence)
            if evidence.terminal is NativeTurnTerminal.INTERRUPTED:
                # An interrupted native turn — however the interruption was
                # initiated, including in the native UI — cancels the
                # remaining queued followups. A normally failed turn does
                # not: later followups may still dispatch after confirmed
                # idle. The cancellation runs only for the first evidence
                # insertion (or the first processing after a crash window):
                # a replay must not cancel followups queued afterwards.
                if first_write or first_processing:
                    await self._cancel_queued_followups(participant_id)
            else:
                self.schedule_dispatch(participant_id)
            return job

    def _finish_from_evidence(
        self, participant_id: str, job_handle: str, evidence: NativeTerminalEvidence
    ) -> Job | None:
        """Finish one job from persisted terminal evidence; exactly once.

        ``JobManager.finish`` is idempotent, so repeated or delayed evidence
        cannot finish the job twice or rewrite its terminal state.
        """
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

    # ---- restart and reconciliation ---------------------------------------

    def fail_undelivered_followups(
        self, participant_ids: list[str], *, error_code: str = DAEMON_RESTARTED_ERROR_CODE
    ) -> list[Job]:
        """Daemon restart: queued/undelivered jobs fail and never replay.

        Every pre-transmission crash window closes here, from durable state
        alone. Every ``RESERVED`` operation — job-bearing and jobless —
        never began transmission, so it settles ``rejected`` and any
        still-running send/queue job finishes ``crashed``; a stranded
        ``RESERVED`` row on a terminal job settles too, so no row is
        immortal. A jobless ``DISPATCHED`` operation is potentially delivered
        and settles ``unknown`` — never retried; job-bearing
        ``DISPATCHED``/accepted/unknown work is left untouched for exact
        reconciliation and never replayed. A running job whose send/queue
        operation already settled ``rejected`` (crash between the settlement
        and the job finish) finishes ``crashed`` from the stored error. A
        native participant's running send job with no operation at all
        (crash between the job write and the reservation) is never a legacy
        job: it finishes ``crashed`` as well. Everything is settled, so
        every row becomes prunable. Nothing is replayed automatically: the
        caller decides what to re-queue.
        """
        failed: list[Job] = []
        for participant_id in participant_ids:
            failed.extend(self._fail_reserved_operations(participant_id, error_code))
            self._settle_jobless_dispatched(participant_id)
            failed.extend(self._fail_queued_followups(participant_id, error_code))
            failed.extend(self._reconcile_running_jobs_at_restart(participant_id, error_code))
        return failed

    def _fail_reserved_operations(self, participant_id: str, error_code: str) -> list[Job]:
        """Settle every RESERVED operation — job-bearing and jobless.

        ``RESERVED`` means transmission never began, so the delivery is
        definitively never made: it settles ``rejected`` and never retries.
        Any still-running send/queue job behind such a row finishes
        ``crashed``; a stranded row on a terminal job settles alone, so no
        row is immortal. A stranded steer/interrupt row never finishes its
        job — the job's own send operation governs its fate.
        """
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

    def _settle_jobless_dispatched(self, participant_id: str) -> None:
        """A jobless DISPATCHED operation is potentially delivered: ``unknown``.

        Job-bearing ``DISPATCHED`` work keeps its exact reconciliation path
        and is never replayed here.
        """
        for operation in self._store.control_operations_in_phases(
            participant_id, (ControlDeliveryPhase.DISPATCHED,)
        ):
            if operation.job_handle is not None:
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

    def _fail_queued_followups(self, participant_id: str, error_code: str) -> list[Job]:
        """Queued followups fail at restart and are never replayed."""
        failed: list[Job] = []
        for operation in self._store.queued_control_operations(participant_id):
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
        """Close the two remaining crash windows around running jobs.

        A running job whose send/queue operation already settled
        ``rejected`` (crash between the settlement and the job finish)
        finishes ``crashed`` from the stored refusal facts. A natively-wired
        participant's running ``send``/``spawn`` job with no operation at
        all (crash between the job write and the reservation) is never a
        legacy job: without an operation it is reachable by no exact
        reconciliation, so it finishes ``crashed`` too — even before the
        runtime manager has adopted the backend, because native-ness is
        classified from the persisted binding, not from the transient
        runtime registry. A legacy op-less job stays with the observer.
        """
        failed: list[Job] = []
        for job in self._store.running_jobs_for_target(participant_id):
            operations = self._store.control_operations_for_job(job.handle)
            if not operations:
                if self._participant_is_native(participant_id) and job.kind in (
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

    def finish_jobs_from_pending_evidence(self, participant_ids: list[str]) -> list[Job]:
        """Close the crash window between the evidence commit and job finish.

        A daemon crash after persisting terminal evidence but before
        finishing the job leaves durable evidence and a still-running job.
        This finishes each such job exactly once from the stored evidence —
        never from an oldest-running guess, never replaying the prompt.
        """
        finished: list[Job] = []
        for participant_id in participant_ids:
            for job in self._store.active_running_jobs_for_target(participant_id):
                for operation in self._store.control_operations_for_job(job.handle):
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
                    result = self._finish_from_evidence(participant_id, job.handle, evidence)
                    if result is not None and result.state != JobState.RUNNING:
                        finished.append(result)
                        break
        return finished

    async def reconcile_ambiguous_delivery(
        self, participant_id: str, *, now_ts: float
    ) -> list[Job]:
        """Resolve DISPATCHED/UNKNOWN-delivery operations — never by retry.

        Reconciliation reads only exact native facts: stored terminal
        evidence, or the authoritative runtime snapshot when the turn is
        still active. An operation unresolved past the deadline finishes its
        job ``crashed`` with ``delivery_unknown`` and an explicit warning
        that native work may have been accepted. No prompt is resent and
        nothing falls back to tmux.
        """
        runtime = self._runtime_for(participant_id)
        resolved: list[Job] = []
        async with self._lock(participant_id):
            snapshot = await runtime.snapshot() if runtime is not None else None
            ambiguous: dict[str, ControlOperation] = {}
            for operation in self._store.dispatched_control_operations(participant_id):
                ambiguous[operation.operation_id] = operation
            for job in self._store.active_running_jobs_for_target(participant_id):
                for operation in self._store.control_operations_for_job(job.handle):
                    if (
                        operation.delivery_phase is ControlDeliveryPhase.SETTLED
                        and operation.delivery_result is DeliveryResult.UNKNOWN
                    ):
                        ambiguous[operation.operation_id] = operation
            for operation in list(ambiguous.values()):
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
        job = self._store.get_job(operation.job_handle) if operation.job_handle else None
        if operation.job_handle and (job is None or job.state != JobState.RUNNING):
            return []  # already finished
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
            # Committed terminal evidence outranks the snapshot: the turn is
            # over, whatever a stale snapshot still reports.
            if operation.job_handle is None:
                return []  # a jobless operation carries no recovery obligation
            result = self._finish_from_evidence(participant_id, operation.job_handle, evidence)
            return [] if result is None else [result]
        if (
            snapshot is not None
            and operation.native_turn_id is not None
            and operation.backend_generation is not None
            and operation.native_session_id is not None
            and snapshot.backend_generation == operation.backend_generation
            and snapshot.native_session_id == operation.native_session_id
            and snapshot.native_turn_id == operation.native_turn_id
        ):
            return []  # the same live turn on the same backend and session; keep waiting
        if now_ts - operation.updated_at < AMBIGUOUS_DELIVERY_DEADLINE_SECONDS:
            return []  # still inside the immediate reconciliation window
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
        return [] if finished is None else [finished]

    # ---- active-job selectors for observation integration ------------------

    def active_jobs(self, participant_id: str) -> list[Job]:
        """Running jobs actually delivered to the participant, oldest first.

        The explicit seam for observation: a queued followup is never
        returned, so it can never become the oldest eligible active job and
        receive transcript results, path touches, or rescue attention by
        accident. The all-running queries stay available for cancellation
        and lifecycle handling.
        """
        return self._store.active_running_jobs_for_target(participant_id)

    def active_job_for_native_turn(
        self,
        participant_id: str,
        *,
        backend_generation: int,
        native_session_id: str,
        native_turn_id: str,
    ) -> Job | None:
        """The exact running job bound to one native turn, or ``None``.

        Fails closed on an ambiguous mapping: ``None`` and no job, never a
        guess. Never falls back to an oldest-running job.
        """
        operation = self._operation_for_turn(
            participant_id=participant_id,
            backend_generation=backend_generation,
            native_session_id=native_session_id,
            native_turn_id=native_turn_id,
        )
        if operation is None or operation.job_handle is None:
            return None
        job = self._store.get_job(operation.job_handle)
        if job is None or job.state != JobState.RUNNING:
            return None
        return job

    def queued_jobs(self, participant_id: str) -> list[Job]:
        """Pending followup jobs in FIFO order — for exclusion, never completion."""
        jobs: list[Job] = []
        for operation in self._store.queued_control_operations(participant_id):
            if operation.job_handle:
                job = self._store.get_job(operation.job_handle)
                if job is not None and job.state == JobState.RUNNING:
                    jobs.append(job)
        return jobs

    # ---- internals ---------------------------------------------------------

    def _lock(self, participant_id: str) -> asyncio.Lock:
        """One lock per participant, and nothing global, ever."""
        return self._locks.setdefault(participant_id, asyncio.Lock())

    def _clock(self) -> float:
        return now()

    def _mint_sequence(self, *, connection=None) -> int:
        """One position from the persisted send-sequence allocator."""
        return self._store.allocate_control_queue_sequence(connection=connection)

    def _mint_operation_id(self, participant_id: str, kind: ControlKind) -> str:
        sequence = self._mint_sequence()
        return f"{participant_id}#{sequence}:{kind.value}"

    def _transport_for(self, participant_id: str) -> ControlTransport:
        """Queue transport from the durable classification, never from the live runtime alone.

        A persisted native binding without a live runtime is still native:
        its queue slots are reserved with ``NATIVE_RUNTIME`` transport, so
        queue/generation facts can never classify a disconnected native
        participant as legacy work.
        """
        if self._participant_is_native(participant_id):
            return ControlTransport.NATIVE_RUNTIME
        return ControlTransport.LEGACY_TMUX

    def _participant_is_native(self, participant_id: str) -> bool:
        """Native by live runtime or by persisted binding wiring — durable truth.

        Restart reconciliation runs before the runtime manager has adopted
        or recreated any runtime, so the transient ``runtime_for`` lookup
        alone would misclassify a natively-wired participant as legacy and
        strand its op-less jobs. The persisted ``RuntimeBinding`` wiring is
        the durable classification; only ``NATIVE`` wiring counts — an
        explicitly legacy binding is legacy.
        """
        if self._runtime_for(participant_id) is not None:
            return True
        binding = self._store.get_runtime_binding(participant_id)
        return binding is not None and binding.wiring is RuntimeWiring.NATIVE

    def _disconnected_native_refusal(self, participant_id: str, control: str) -> StaleTarget:
        """A persisted native binding whose runtime is gone: fail closed.

        The participant is natively wired — a detached or recovering native
        — so its controls are never delivered, queued, or retried through
        the legacy pane. The refusal is actionable and unambiguous: there is
        no fallback and no automatic retry; the runtime reconnects through
        reconcile/adoption or the participant is restarted, and only then
        can the caller issue the control again.
        """
        return StaleTarget(
            f"participant {participant_id!r} is natively wired but its runtime is "
            f"not connected (detached or recovering); the {control} is refused and "
            "never falls back to the legacy pane, is never queued as legacy work, "
            "and is never retried automatically — wait for the runtime to "
            "reconnect (reconcile/adopt) or restart the participant, then issue "
            "the control again"
        )

    def _create_send_job(
        self,
        participant_id: str,
        *,
        caller_id: str,
        prompt: str,
        response_format: str | None,
    ) -> Job:
        handle = f"{participant_id}#{self._mint_sequence()}"
        self._jobs.create(
            handle=handle,
            caller_id=caller_id,
            target_id=participant_id,
            kind="send",
            prompt=prompt,
            cwd=self._gates.cwd_for(participant_id),
            response_format=response_format,
        )
        return self._require_job(handle)

    def _reusable_spawn_job(
        self,
        participant_id: str,
        *,
        job_handle: str,
        caller_id: str,
        prompt: str,
        response_format: str | None,
    ) -> Job:
        """Validate a job handle for native initial-dispatch reuse.

        The handle must name the existing ``RUNNING`` ``spawn`` job of
        exactly this target, caller, prompt, and response-format contract.
        Any mismatch fails closed before transmission — nothing is minted
        or sent, and the job is left exactly as it was, so the caller's own
        error handling owns it.
        """
        job = self._store.get_job(job_handle)
        if job is None:
            raise BadRequest(
                f"job {job_handle!r} does not exist; the initial dispatch of "
                f"participant {participant_id!r} cannot reuse it"
            )
        if job.kind != "spawn":
            raise BadRequest(
                f"job {job_handle!r} is a {job.kind!r} job, not the spawn job of "
                f"participant {participant_id!r}; initial-dispatch reuse accepts "
                "exactly the spawn job and creates no second job"
            )
        if job.state != JobState.RUNNING:
            raise BadRequest(
                f"job {job_handle!r} is already {job.state}; the initial "
                f"dispatch of participant {participant_id!r} can only reuse a "
                "running spawn job"
            )
        if job.target_id != participant_id:
            raise BadRequest(
                f"job {job_handle!r} belongs to target {job.target_id!r}, not "
                f"{participant_id!r}; refusing to dispatch another participant's "
                "spawn job"
            )
        if job.caller_id != caller_id:
            raise BadRequest(
                f"job {job_handle!r} was created by caller {job.caller_id!r}, not "
                f"{caller_id!r}; the initial dispatch must keep the spawn's caller "
                "contract"
            )
        if (job.prompt or "") != prompt:
            raise BadRequest(
                f"job {job_handle!r} carries a different prompt than the one being "
                f"dispatched to participant {participant_id!r}; the initial "
                "dispatch must be exactly the spawn's prompt"
            )
        if job.response_format != response_format:
            raise BadRequest(
                f"job {job_handle!r} carries response_format "
                f"{job.response_format!r}, not {response_format!r}; the initial "
                "dispatch must keep the spawn's response-format contract"
            )
        if self._store.control_operations_for_job(job_handle):
            # The initial dispatch happens exactly once. Any operation row
            # already tied to this spawn job — whatever its phase or result —
            # means a prior attempt exists: RESERVED is failed at restart,
            # DISPATCHED/ACCEPTED/UNKNOWN is reconciled from exact native
            # facts, REJECTED is terminal. There is no legitimate second
            # attempt, so this fails closed before the snapshot, before any
            # minting, and before any transmission.
            raise BadRequest(
                f"job {job_handle!r} already carries a control operation; the "
                f"initial dispatch of participant {participant_id!r} happens "
                "exactly once and is never retransmitted"
            )
        return job

    def _require_job(self, handle: str) -> Job:
        job = self._store.get_job(handle)
        assert job is not None
        return job

    def _reserve(
        self,
        operation_id: str,
        *,
        participant_id: str,
        kind: ControlKind,
        transport: ControlTransport,
        phase: ControlDeliveryPhase,
        job_handle: str | None = None,
        backend_generation: int | None = None,
        native_session_id: str | None = None,
        native_turn_id: str | None = None,
        queue_sequence: int | None = None,
        payload: str | None = None,
        connection=None,
    ) -> None:
        """Persist the operation before transmission; the id is its identity."""
        timestamp = self._clock()
        self._store.reserve_control_operation(
            _operation_row(
                operation_id=operation_id,
                participant_id=participant_id,
                kind=kind,
                transport=transport,
                phase=phase,
                job_handle=job_handle,
                backend_generation=backend_generation,
                native_session_id=native_session_id,
                native_turn_id=native_turn_id,
                queue_sequence=queue_sequence,
                payload=payload,
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=connection,
        )

    async def _deliver_native(
        self,
        runtime: HarnessRuntime,
        *,
        participant_id: str,
        operation_id: str,
        prompt: str,
        job_handle: str,
        snapshot: RuntimeSnapshot,
    ) -> None:
        """DISPATCHED before transmission; settle from the receipt; no retry.

        If the acknowledgement is lost, the operation stays ``DISPATCHED``
        and the job stays running — the delivery is potentially delivered,
        eligible only for reconciliation. No resend, no tmux fallback.
        """
        self._store.mark_control_operation_dispatched(
            operation_id,
            native_session_id=snapshot.native_session_id,
            updated_at=self._clock(),
        )
        try:
            receipt = await runtime.send(operation_id=operation_id, prompt=prompt)
        except Exception as exc:
            logger.warning(
                "delivery of %s to %s is uncertain (acknowledgement lost): %s; "
                "no retry, no tmux fallback",
                operation_id,
                participant_id,
                exc,
            )
            return
        if not self._receipt_names_operation(operation_id, receipt):
            # A receipt for another operation cannot settle this one as
            # accepted or rejected; the delivery stays uncertain, the job
            # stays running, and deadline reconciliation is the only close.
            self._settle_uncertain(
                operation_id,
                error=(
                    f"the native receipt named operation {receipt.operation_id!r}, "
                    f"not {operation_id!r}; the delivery is uncertain and the "
                    "receipt is not trusted to settle it"
                ),
            )
            return
        if receipt.result is DeliveryResult.REJECTED:
            self._settle_from_receipt(operation_id, receipt)
            self._jobs.finish(
                job_handle,
                state=JobState.CRASHED,
                result=receipt.error or "the native backend refused the prompt",
                error_code=receipt.error_code or SEND_REJECTED_ERROR_CODE,
            )
        elif receipt.result is DeliveryResult.ACCEPTED:
            if receipt.native_turn_id is None:
                # Accepted but uncorrelated: without a native turn id the
                # job can never be finished by evidence. Keep no-retry
                # semantics and classify as uncertain so the ambiguous-
                # delivery deadline closes it.
                self._settle_uncertain(
                    operation_id,
                    error=(
                        "the native backend accepted the prompt but reported no "
                        "native turn id; the accepted turn cannot be correlated, "
                        "so the delivery stays uncertain"
                    ),
                )
                return
            # Check the binding before settling this operation's own turn:
            # once two operations carry the same native turn, the exact
            # lookup is ambiguous and evidence could reach neither job.
            if self._turn_is_bound_to_another_job(participant_id, snapshot, receipt, job_handle):
                self._store.settle_control_operation(
                    operation_id,
                    result=DeliveryResult.REJECTED,
                    error_code=NATIVE_TURN_CONFLICT_ERROR_CODE,
                    error=(
                        f"native turn {receipt.native_turn_id!r} is already "
                        "bound to another Theater job"
                    ),
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
            else:
                self._settle_from_receipt(operation_id, receipt)
        else:
            # An uncertain delivery settles UNKNOWN with the turn it named,
            # if any: never retried, never tmux-fallback, eligible only for
            # exact evidence or snapshot reconciliation.
            self._settle_from_receipt(operation_id, receipt)
            if receipt.result is DeliveryResult.UNKNOWN and snapshot.native_session_id is not None:
                logger.warning(
                    "delivery of %s to %s stayed uncertain (turn %s); no retry, "
                    "no tmux fallback — evidence or the snapshot is the only path",
                    operation_id,
                    participant_id,
                    receipt.native_turn_id,
                )

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

    def _settle_uncertain(self, operation_id: str, *, error: str) -> None:
        """Settle one operation as uncertain: never retried, never fallback."""
        self._store.settle_control_operation(
            operation_id,
            result=DeliveryResult.UNKNOWN,
            error_code=DELIVERY_UNKNOWN_ERROR_CODE,
            error=error,
            updated_at=self._clock(),
        )

    def _settle_from_receipt(self, operation_id: str, receipt: ControlReceipt) -> None:
        self._store.settle_control_operation(
            operation_id,
            result=receipt.result,
            native_turn_id=receipt.native_turn_id,
            error_code=receipt.error_code,
            error=receipt.error,
            updated_at=self._clock(),
        )

    def _turn_is_bound_to_another_job(
        self,
        participant_id: str,
        snapshot: RuntimeSnapshot,
        receipt: ControlReceipt,
        job_handle: str,
    ) -> bool:
        """Never bind two Theater jobs to one native turn; fail closed.

        The serialized idle check makes this unreachable through Theater
        alone; the accepted native-UI race can absorb a prompt into a
        UI-started turn, which no Theater job owns. A conflicting or already
        ambiguous job-bearing mapping is therefore a bug state: the newer
        job must close ``crashed`` instead of settling into an ambiguous
        mapping. Ambiguity fails the newer job closed too — settling an
        accepted row into an already-ambiguous turn would poison the exact
        lookup for every job that already maps to it.
        """
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
        """Fails-closed capability gate at execution; no fallback ever.

        The recorded reason is returned verbatim: an unsupported or gated
        capability refuses the control instead of degrading it to a
        different transport or a retry.
        """
        if snapshot.capabilities.supports(capability):
            return
        reason = snapshot.capabilities.reason_for(capability)
        raise BadRequest(
            f"participant {participant_id!r} does not support {action} "
            f"({reason}); the native runtime gates this capability, so the "
            "control is refused — never retried, never fallen back"
        )

    def _reject_busy(
        self,
        participant_id: str,
        snapshot: RuntimeSnapshot,
        *,
        idle_only: bool = False,
        exclude: str | None = None,
    ) -> None:
        """Authoritative idle/busy check from the runtime snapshot and store.

        ``exclude`` names the one job this dispatch is reusing — its own
        op-less running row (a native spawn job before its SEND operation is
        reserved) must not read as *another* active job. Every other job,
        queued followup, active turn, and pending interaction still refuses.
        """
        if snapshot.pending_interaction is not None:
            raise AwaitingDecision(
                f"participant {participant_id!r} is waiting for a human to answer "
                f"a native {snapshot.pending_interaction.kind.value}; only the "
                "native UI may answer it — not Theater, not the caller"
            )
        queued = self._store.queued_control_operation_count(participant_id)
        if queued:
            raise Busy(
                f"participant {participant_id!r} has {queued} queued followup(s); "
                "an ordinary send cannot jump ahead of them — await the queued "
                "handles or queue another followup instead"
            )
        if snapshot.native_turn_id is not None:
            raise Busy(
                f"participant {participant_id!r} is working on native turn "
                f"{snapshot.native_turn_id!r}; not injecting a new prompt"
                + ("" if idle_only else ". Call interrupt, wait for idle, or queue a followup")
            )
        active = self._store.active_running_jobs_for_target(participant_id)
        if exclude is not None:
            active = [job for job in active if job.handle != exclude]
        if active:
            raise Busy(
                f"participant {participant_id!r} has a running send job "
                f"({active[0].handle}); not injecting a new prompt"
            )

    def _operation_for_snapshot_turn(self, participant_id: str, snapshot: RuntimeSnapshot):
        if snapshot.native_session_id is None or snapshot.native_turn_id is None:
            return None
        return self._operation_for_turn(
            participant_id=participant_id,
            backend_generation=snapshot.backend_generation,
            native_session_id=snapshot.native_session_id,
            native_turn_id=snapshot.native_turn_id,
        )

    def _operation_for_turn(
        self,
        *,
        participant_id: str,
        backend_generation: int,
        native_session_id: str,
        native_turn_id: str,
    ):
        """The exact-turn lookup — the only job-to-turn mapping there is."""
        try:
            return self._store.control_operation_for_native_turn(
                participant_id=participant_id,
                backend_generation=backend_generation,
                native_session_id=native_session_id,
                native_turn_id=native_turn_id,
            )
        except ControlOperationAmbiguityError as exc:
            # A duplicate job-bearing mapping is a bug state; failing closed
            # means completing nothing, never guessing.
            logger.error(  # noqa: TRY400 - a controlled fail-closed, not a crash
                "native turn %s of %s maps to multiple job-bearing operations; failing closed: %s",
                native_turn_id,
                participant_id,
                exc,
            )
            return None


def _error_code_of(exc: Exception) -> str:
    return getattr(exc, "code", None) or "dispatch_failed"


def _operation_row(
    *,
    operation_id: str,
    participant_id: str,
    kind: ControlKind,
    transport: ControlTransport,
    phase: ControlDeliveryPhase,
    created_at: float,
    updated_at: float,
    job_handle: str | None = None,
    backend_generation: int | None = None,
    native_session_id: str | None = None,
    native_turn_id: str | None = None,
    queue_sequence: int | None = None,
    payload: str | None = None,
) -> ControlOperation:
    return ControlOperation(
        operation_id=operation_id,
        participant_id=participant_id,
        kind=kind,
        transport=transport,
        delivery_phase=phase,
        job_handle=job_handle,
        backend_generation=backend_generation,
        native_session_id=native_session_id,
        native_turn_id=native_turn_id,
        queue_sequence=queue_sequence,
        payload=payload,
        created_at=created_at,
        updated_at=updated_at,
    )
