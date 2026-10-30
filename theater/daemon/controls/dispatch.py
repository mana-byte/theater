"""Followup queue dispatch: draining the queue head over its selected route."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass

from theater.constants.daemon import CONTROL_QUEUE_MAX_PENDING
from theater.daemon.controls._common import (
    ACTION_QUEUE_DISPATCH,
    ACTION_SEND,
    QUEUE_ORIGIN_SEND,
    SEND_REJECTED_ERROR_CODE,
    _error_code_of,
)
from theater.daemon.controls._host import ControlHost
from theater.daemon.controls.provider_delivery import ProviderPresenceDeferred
from theater.daemon.controls.routing import ControlRoute
from theater.daemon.persistence.repositories.control_operations import ControlOperation
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    DeliveryResult,
    HarnessRuntime,
    RuntimeCapability,
)
from theater.models import (
    AwaitingDecision,
    BadRequest,
    Busy,
    HumanPresent,
    Job,
    JobState,
    NotAddressable,
    StaleTarget,
)

logger = logging.getLogger("theater.daemon.controls")

#: Refusals that are temporary: a queued followup hit by one stays queued.
TEMPORARY_REFUSALS = (Busy, HumanPresent, AwaitingDecision)


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
    #: True when only this head cannot go now; the next sender's head may still be tried.
    skip: bool = False


class DispatchControls(ControlHost):
    """Dispatch the followup queue head when the participant is idle."""

    def schedule_dispatch(self, participant_id: str) -> None:
        """Try to dispatch the queue head on the next scheduling opportunity."""
        self._schedule_maintenance(participant_id)
        if self._closing:
            return
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
        """Run one dispatch pass so its crash is logged, never unretrieved."""
        try:
            outcome = await self.dispatch_queue(participant_id)
        except Exception:
            logger.exception(
                "queue dispatch pass for %s crashed; no retry — a later "
                "scheduling opportunity will run another pass",
                participant_id,
            )
            return QueueDispatchOutcome()
        else:
            if self._scheduler_started and self._has_maintenance_work(participant_id):
                self._schedule_maintenance(participant_id)
            return outcome

    async def dispatch_queue(self, participant_id: str) -> QueueDispatchOutcome:
        """Dispatch queue items one at a time, after an authoritative idle check."""
        dispatched: list[str] = []
        failed: list[tuple[str, str]] = []
        deferred = False
        async with self._lock(participant_id):
            # An internal repair outranks every queued followup, and a fence holds them all back.
            await self._advance_repairs_locked(participant_id)
            if self._store.repair_fence_active(participant_id):
                return QueueDispatchOutcome(deferred=True)
            skipped: set[str] = set()
            while True:
                heads = [
                    head
                    for head in self._lane_heads(participant_id)
                    if head.operation_id not in skipped
                ]
                if not heads:
                    break
                head = heads[0]
                outcome = await self._dispatch_head(participant_id, head)
                dispatched.extend(outcome.dispatched)
                failed.extend(outcome.failed)
                if outcome.deferred:
                    deferred = True
                if outcome.dispatched or outcome.failed:
                    self._control_notifier.notify(head.operation_id)
                if outcome.skip:
                    skipped.add(head.operation_id)
                    continue
                if not outcome.dispatched and not outcome.failed:
                    break
                # A definitive failure removed one item; try the next.
                if outcome.dispatched:
                    break
        return QueueDispatchOutcome(
            dispatched=tuple(dispatched), failed=tuple(failed), deferred=deferred
        )

    def _check_gate(
        self, head: ControlOperation, job: Job
    ) -> tuple[Job, QueueDispatchOutcome | None]:
        """A message waiting on a source job is not dispatchable yet.

        It keeps its lane head, so nothing the same sender queued later overtakes it, while other
        senders' lanes go on. Opening the gate renders the source card into the job's prompt.
        A message bound to a play whose binding no longer holds fails here, never delivered.
        """
        if self._gate_opener is not None:
            readiness = self._gate_opener.ready_for_dispatch(job)
            if readiness.waiting:
                return job, QueueDispatchOutcome(skip=True)
            if readiness.error is not None:
                return job, self._fail_queued_item(head, job, readiness.error)
            job = self._store.get_job(job.handle) or job
        refusal = None if self._play_gate is None else self._play_gate.dispatch_refusal(job.handle)
        if refusal is not None:
            return job, self._fail_queued_item(head, job, refusal)
        return job, None

    def _lane_heads(self, participant_id: str) -> list[ControlOperation]:
        """Each requester's oldest queued row, in send order: one sender never overtakes itself.

        Scans at most the queue bound; a row whose job vanished heads a lane of its own.
        """
        callers = self._store.queued_control_callers(participant_id)
        seen: set[str] = set()
        heads: list[ControlOperation] = []
        for operation in self._store.queued_control_operations(participant_id)[
            :CONTROL_QUEUE_MAX_PENDING
        ]:
            lane = callers.get(operation.operation_id) or operation.operation_id
            if lane not in seen:
                seen.add(lane)
                heads.append(operation)
        return heads

    async def _dispatch_head(
        self, participant_id: str, head: ControlOperation
    ) -> QueueDispatchOutcome:
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
            logger.warning("queued %s dropped: job %s missing", head.operation_id, head.job_handle)
            return QueueDispatchOutcome(failed=((head.job_handle or "", "job_missing"),))
        caller_id = job.caller_id
        if caller_id is None:
            return self._fail_queued_item(
                head,
                job,
                BadRequest(f"queued job {job.handle!r} has no caller identity"),
            )
        job, blocked = self._check_gate(head, job)
        if blocked is not None:
            return blocked
        # A peer's queued send authorizes as the open ordinary send it is; other rows are parent
        # controls and are re-authorized against the current owner.
        action = (
            ACTION_SEND
            if self._queue_origin(head) == QUEUE_ORIGIN_SEND
            else (ACTION_QUEUE_DISPATCH)
        )
        try:
            self._gates.authorize(participant_id, caller_id, action)
        except Exception as exc:
            return self._fail_queued_item(head, job, exc)
        try:
            await self._gates.require_absent(participant_id)
            await self._gates.send_preflight(participant_id)
        except TEMPORARY_REFUSALS as exc:
            logger.debug("queued followup %s deferred: %s", head.operation_id, exc)
            return QueueDispatchOutcome(deferred=True)
        except Exception as exc:
            # A preflight failure never fails a protected queue: recheck
            # presence first; an unprotected head classifies as before.
            try:
                await self._gates.require_absent(participant_id)
            except Exception:
                logger.debug("queued followup %s deferred: %s", head.operation_id, exc)
                return QueueDispatchOutcome(deferred=True)
            return self._fail_queued_item(head, job, exc)
        route = self.route_for(participant_id, RuntimeCapability.QUEUE_FOLLOWUP)
        if route.transport is None:
            return self._fail_queued_item(
                head,
                job,
                BadRequest(f"participant {participant_id!r} no longer offers a followup transport"),
            )
        runtime = self._runtime_for(participant_id)
        if head.provider_id is not None:
            if not route.is_provider:
                return self._fail_queued_item(
                    head,
                    job,
                    StaleTarget(
                        f"provider route for participant {participant_id!r} changed before "
                        "queued delivery; the prompt is never failed over"
                    ),
                )
            return await self._dispatch_head_provider(participant_id, head, job, route)
        if route.is_native:
            if runtime is not None:
                return await self._dispatch_head_native(runtime, participant_id, head, job)
            logger.info(
                "queued followup %s of %s deferred: the natively-wired "
                "participant's runtime is not connected; no legacy pane delivery",
                head.operation_id,
                participant_id,
            )
            return QueueDispatchOutcome(deferred=True)
        return await self._refuse_unbound_queued(participant_id, head, job)

    async def _dispatch_head_provider(
        self,
        participant_id: str,
        head: ControlOperation,
        job: Job,
        route: ControlRoute,
    ) -> QueueDispatchOutcome:
        if self._presence_retry_after(head) > self._clock():
            return QueueDispatchOutcome(deferred=True, skip=True)
        try:
            terminal = self._provider.require(
                participant_id, RuntimeCapability.QUEUE_FOLLOWUP, route
            )
            modal_check = self._gates.provider_modal_check
            if modal_check is not None:
                # A modal refusal is temporary: the human answers, dispatch retries.
                await modal_check(participant_id)
        except TEMPORARY_REFUSALS as exc:
            logger.debug("queued followup %s deferred: %s", head.operation_id, exc)
            return QueueDispatchOutcome(deferred=True)
        except Exception as exc:
            return self._fail_queued_item(head, job, exc)
        pinned = (
            head.provider_id,
            head.provider_generation,
            head.terminal_id,
            head.terminal_incarnation,
        )
        current = (
            terminal.provider_id,
            terminal.provider_generation,
            terminal.terminal_id,
            terminal.terminal_incarnation,
        )
        if pinned != current:
            return self._fail_queued_item(
                head,
                job,
                StaleTarget(
                    f"provider terminal identity for participant {participant_id!r} changed; "
                    "the queued prompt is never replayed or failed over"
                ),
            )
        if self._store.has_execution_barrier(participant_id):
            return QueueDispatchOutcome(deferred=True)
        queued_handles = {
            operation.job_handle
            for operation in self._store.queued_control_operations(participant_id)
            if operation.job_handle is not None
        }
        if any(
            candidate.handle not in queued_handles
            for candidate in self._store.running_jobs_for_target(participant_id)
        ):
            return QueueDispatchOutcome(deferred=True)
        callback_operation_id = self._callback_operation_id(head) or head.operation_id
        public = self._store.operations.get(callback_operation_id)
        public_control = public is not None and public.control_operation_id == head.operation_id
        try:
            result = await self._provider.deliver(
                route,
                capability=RuntimeCapability.QUEUE_FOLLOWUP,
                kind=ControlKind.QUEUE_FOLLOWUP,
                participant_id=participant_id,
                control_operation_id=head.operation_id,
                callback_operation_id=callback_operation_id,
                action={"kind": "submit_text", "text": job.prompt or ""},
                job_handle=job.handle,
                # A public operation owns its original control ID and must see its refusal.
                defer_on_presence=not public_control,
            )
        except ProviderPresenceDeferred as exc:
            self._provider.defer_presence(head.operation_id, error=str(exc))
            self._jobs.detach_touch_accumulator(job.handle)
            return QueueDispatchOutcome(deferred=True)
        if result is DeliveryResult.REJECTED:
            return QueueDispatchOutcome(failed=((job.handle, SEND_REJECTED_ERROR_CODE),))
        return QueueDispatchOutcome(dispatched=(job.handle,))

    @staticmethod
    def _presence_retry_after(head: ControlOperation) -> float:
        try:
            payload = json.loads(head.payload or "{}")
        except (TypeError, ValueError):
            return 0.0
        value = payload.get("presence_retry_after") if isinstance(payload, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return float(value)
        return 0.0

    async def _refuse_unbound_queued(
        self,
        participant_id: str,
        head: ControlOperation,
        job: Job,
    ) -> QueueDispatchOutcome:
        """Record the historical route, then refuse after the presence recheck."""
        if head.transport is not ControlTransport.TERMINAL_FALLBACK:
            selected = self._select_queued_route(
                head,
                transport=ControlTransport.TERMINAL_FALLBACK,
                backend_generation=None,
                native_session_id=None,
                payload=None,
            )
            if selected is None:
                return QueueDispatchOutcome(deferred=True, skip=True)
            head = selected
        try:
            await self._gates.require_absent(participant_id)
        except TEMPORARY_REFUSALS as exc:
            logger.debug("queued followup %s deferred: %s", head.operation_id, exc)
            return QueueDispatchOutcome(deferred=True)
        except Exception as exc:
            return self._fail_queued_item(head, job, exc)
        return self._fail_queued_item(
            head,
            job,
            NotAddressable(
                f"participant {participant_id!r} has no current terminal-provider route"
            ),
        )

    async def _dispatch_head_native(
        self,
        runtime: HarnessRuntime,
        participant_id: str,
        head: ControlOperation,
        job: Job,
    ) -> QueueDispatchOutcome:
        # Protected heads stay queued, including protection acquired during the snapshot.
        try:
            snapshot = await self._snapshot_for_control(runtime, participant_id)
        except TEMPORARY_REFUSALS as exc:
            logger.debug("queued followup %s deferred: %s", head.operation_id, exc)
            return QueueDispatchOutcome(deferred=True)
        try:
            route = self._require_current_native_route(
                participant_id,
                RuntimeCapability.QUEUE_FOLLOWUP,
                snapshot,
                require_available=False,
            )
            # Native delivery needs SEND: the followup queue is Theater-owned, and QUEUE_FOLLOWUP
            # marks forbidden native queue use, so it never gates this path.
            self._require_capability(participant_id, snapshot, RuntimeCapability.SEND, "send")
        except Exception as exc:
            return self._fail_queued_item(head, job, exc)
        if not route.route_available:
            logger.debug(
                "queued followup %s deferred: its exact native route is disconnected",
                head.operation_id,
            )
            return QueueDispatchOutcome(deferred=True)
        # ``UNKNOWN``/disconnected/missing identity are not idle.
        if not self._is_authoritatively_idle(snapshot):
            predecessor = self._queue_predecessor(participant_id, snapshot)
            if predecessor is not None:
                # A human can start another turn while Theater's FIFO is pending.
                self._bind_queued_predecessor(participant_id, snapshot, predecessor)
            return QueueDispatchOutcome(deferred=True)
        self._clear_execution_barriers_from_idle_snapshot(participant_id, snapshot)
        if self._store.has_execution_barrier(participant_id):
            return QueueDispatchOutcome(deferred=True)
        if self._store.active_running_jobs_for_target(participant_id):
            return QueueDispatchOutcome(deferred=True)
        if head.transport is not ControlTransport.NATIVE_RUNTIME:
            selected = self._select_queued_route(
                head,
                transport=ControlTransport.NATIVE_RUNTIME,
                backend_generation=snapshot.backend_generation,
                native_session_id=snapshot.native_session_id,
                payload=None,
            )
            if selected is None:
                return QueueDispatchOutcome(deferred=True, skip=True)
            head = selected
        if (
            head.backend_generation is None
            or head.native_session_id is None
            or head.backend_generation != snapshot.backend_generation
            or head.native_session_id != snapshot.native_session_id
        ):
            return self._fail_queued_item(
                head,
                job,
                StaleTarget(
                    f"the native session of {participant_id!r} changed "
                    f"(reserved generation/session {head.backend_generation}/"
                    f"{head.native_session_id!r}, now {snapshot.backend_generation}/"
                    f"{snapshot.native_session_id!r}); the queued followup is never "
                    "replayed into another session"
                ),
            )
        cwd = self._gates.cwd_for(participant_id)
        if cwd is not None:
            self._jobs.attach_touch_accumulator(job.handle, cwd=cwd)
        await self._deliver_native(
            runtime,
            kind=ControlKind.QUEUE_FOLLOWUP,
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

    def _select_queued_route(
        self,
        operation: ControlOperation,
        *,
        transport: ControlTransport,
        backend_generation: int | None,
        native_session_id: str | None,
        payload: str | None,
    ) -> ControlOperation | None:
        if not self._store.set_queued_control_route(
            operation.operation_id,
            transport=transport,
            backend_generation=backend_generation,
            native_session_id=native_session_id,
            payload=payload,
            updated_at=self._clock(),
        ):
            return None
        selected = self._store.get_control_operation(operation.operation_id)
        return (
            selected
            if selected is not None and selected.delivery_phase is ControlDeliveryPhase.QUEUED
            else None
        )
