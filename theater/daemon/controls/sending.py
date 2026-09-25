"""Ordinary send: native, provider, and legacy prompt delivery."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from theater.daemon.controls._common import (
    ACTION_SEND,
    CONTROL_DELIVERY_ACCEPTED,
    NativeControlPreparation,
    _delivery_label,
    prepare_native_control,
)
from theater.daemon.controls._host import ControlHost
from theater.daemon.controls.busy import BusyOperation
from theater.daemon.controls.routing import ControlRoute
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    DeliveryResult,
    HarnessRuntime,
    RuntimeCapability,
    RuntimeSnapshot,
)
from theater.models import BadRequest, Busy, Job, JobState, NotAddressable, StaleTarget, Status


@dataclass(frozen=True, slots=True)
class _SendRequest:
    participant_id: str
    caller_id: str
    prompt: str
    response_format: str | None
    job_handle: str | None
    operation_id: str | None
    callback_operation_id: str | None
    on_reserved: Callable[[str, str | None], None] | None
    actor_client_id: str | None
    actor_participant_id: str | None
    pre_reserved: bool


class SendControls(ControlHost):
    """Deliver one prompt to an idle participant."""

    async def send(
        self,
        participant_id: str,
        *,
        caller_id: str,
        prompt: str,
        response_format: str | None = None,
        job_handle: str | None = None,
        operation_id: str | None = None,
        callback_operation_id: str | None = None,
        on_reserved: Callable[[str, str | None], None] | None = None,
        actor_client_id: str | None = None,
        actor_participant_id: str | None = None,
        pre_reserved: bool = False,
    ) -> Job:
        """Ordinary send — or the native initial dispatch of one spawn job."""
        with self._control_latency(ControlKind.SEND, participant_id) as latency:
            job, delivery, transport = await self._send(
                _SendRequest(
                    participant_id,
                    caller_id,
                    prompt,
                    response_format,
                    job_handle,
                    operation_id,
                    callback_operation_id,
                    on_reserved,
                    actor_client_id,
                    actor_participant_id,
                    pre_reserved,
                )
            )
            latency.delivery = delivery
            latency.transport = transport
            return job

    async def _send(self, request: _SendRequest) -> tuple[Job, str, str]:
        """The send body; returns its job, delivery label, and transport."""
        participant_id = request.participant_id
        runtime = self._runtime_for(participant_id)
        async with self._lock(participant_id):
            route, initial_dispatch = await self._prepare_send(request)
            if route.is_provider:
                return await self._send_provider(request, route, initial_dispatch=initial_dispatch)
            if route.is_legacy:
                if request.job_handle is not None:
                    raise BadRequest(
                        f"reusing job {request.job_handle!r} for the initial dispatch of "
                        f"participant {participant_id!r} requires native runtime "
                        "wiring; its harness has no runtime, so the prompt can only "
                        "be sent as an ordinary send"
                    )
                legacy_job = await self._send_legacy(
                    participant_id,
                    caller_id=request.caller_id,
                    prompt=request.prompt,
                    response_format=request.response_format,
                    operation_id=request.operation_id,
                    on_reserved=request.on_reserved,
                    actor_client_id=request.actor_client_id,
                    actor_participant_id=request.actor_participant_id,
                )
                return legacy_job, CONTROL_DELIVERY_ACCEPTED, ControlTransport.LEGACY_TMUX.value
            if not route.is_native:
                raise NotAddressable(
                    f"participant {participant_id!r} does not offer a transport for sending"
                )
            return await self._send_native(
                request, runtime, route, initial_dispatch=initial_dispatch
            )

    async def _prepare_send(self, request: _SendRequest) -> tuple[ControlRoute, bool]:
        participant_id = request.participant_id
        self._gates.authorize(participant_id, request.caller_id, ACTION_SEND)
        route = self.route_for(participant_id, RuntimeCapability.SEND)
        if route.transport is None:
            raise NotAddressable(
                f"participant {participant_id!r} does not offer a transport for sending"
            )
        initial_dispatch = request.job_handle is not None
        if not initial_dispatch:
            await self._require_absent(participant_id, route)
        self._gates.check_prompt(request.prompt)
        await self._gates.send_preflight(participant_id)
        return route, initial_dispatch

    async def _send_provider(
        self, request: _SendRequest, route: ControlRoute, *, initial_dispatch: bool
    ) -> tuple[Job, str, str]:
        participant_id = request.participant_id
        reserved = (
            self._require_public_reservation(
                request.operation_id,
                participant_id=participant_id,
                kind=ControlKind.SEND,
                phase=ControlDeliveryPhase.RESERVED,
                route=route,
            )
            if request.pre_reserved
            else None
        )
        if reserved is not None:
            assert reserved.job_handle is not None
            provider_job = self._require_job(reserved.job_handle)
        elif request.job_handle is not None:
            provider_job = self._reusable_spawn_job(
                participant_id,
                job_handle=request.job_handle,
                caller_id=request.caller_id,
                prompt=request.prompt,
                response_format=request.response_format,
            )
        else:
            await self._gates.legacy_busy_check(participant_id)
            self._reject_provider_send_busy(participant_id, exclude=None)
            provider_job = self._create_send_job(
                participant_id,
                caller_id=request.caller_id,
                prompt=request.prompt,
                response_format=request.response_format,
                actor_client_id=request.actor_client_id,
                actor_participant_id=request.actor_participant_id,
            )
        if not initial_dispatch:
            self._reject_provider_send_busy(participant_id, exclude=provider_job.handle)
        control_id = request.operation_id or self._mint_operation_id(
            participant_id, ControlKind.SEND
        )
        if reserved is None:
            self._provider.reserve(
                control_id,
                route,
                participant_id=participant_id,
                kind=ControlKind.SEND,
                phase=ControlDeliveryPhase.RESERVED,
                job_handle=provider_job.handle,
            )
            self._notify_reserved(request.on_reserved, control_id, provider_job.handle)
        provider_delivery = await self._provider.deliver(
            route,
            capability=RuntimeCapability.SEND,
            kind=ControlKind.SEND,
            participant_id=participant_id,
            control_operation_id=control_id,
            callback_operation_id=request.callback_operation_id or control_id,
            action={"kind": "submit_text", "text": request.prompt},
            job_handle=provider_job.handle,
        )
        return (
            self._require_job(provider_job.handle),
            _delivery_label(provider_delivery),
            ControlTransport.PROVIDER_TERMINAL.value,
        )

    async def _send_native(
        self,
        request: _SendRequest,
        runtime: HarnessRuntime | None,
        route: ControlRoute,
        *,
        initial_dispatch: bool,
    ) -> tuple[Job, str, str]:
        participant_id = request.participant_id
        reserved = (
            self._require_public_reservation(
                request.operation_id,
                participant_id=participant_id,
                kind=ControlKind.SEND,
                phase=ControlDeliveryPhase.RESERVED,
                route=route,
            )
            if request.pre_reserved
            else None
        )
        job: Job | None = None
        if reserved is not None:
            assert reserved.job_handle is not None
            job = self._require_job(reserved.job_handle)
        elif request.job_handle is not None:
            job = self._reusable_spawn_job(
                participant_id,
                job_handle=request.job_handle,
                caller_id=request.caller_id,
                prompt=request.prompt,
                response_format=request.response_format,
            )
        prepared = await prepare_native_control(
            self,
            runtime,
            NativeControlPreparation(
                participant_id=participant_id,
                route_capability=RuntimeCapability.SEND,
                required_capability=RuntimeCapability.SEND,
                action=ACTION_SEND,
                refusal_label=ACTION_SEND,
                initial_dispatch=initial_dispatch,
            ),
        )
        snapshot = prepared.snapshot
        if reserved is not None:
            self._require_reserved_native_identity(reserved, snapshot)
        self._reject_busy(
            participant_id,
            snapshot,
            operation=BusyOperation.SEND,
            exclude=job.handle if job is not None else None,
        )
        if job is None:
            job = self._create_send_job(
                participant_id,
                caller_id=request.caller_id,
                prompt=request.prompt,
                response_format=request.response_format,
                actor_client_id=request.actor_client_id,
                actor_participant_id=request.actor_participant_id,
            )
        operation_id = request.operation_id or self._mint_operation_id(
            participant_id, ControlKind.SEND
        )
        if reserved is None:
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
            self._notify_reserved(request.on_reserved, operation_id, job.handle)
        cwd = self._gates.cwd_for(participant_id)
        if request.pre_reserved and cwd is not None:
            self._jobs.attach_touch_accumulator(job.handle, cwd=cwd)
        delivery = await self._deliver_native(
            prepared.runtime,
            kind=ControlKind.SEND,
            participant_id=participant_id,
            operation_id=operation_id,
            prompt=request.prompt,
            job_handle=job.handle,
            snapshot=snapshot,
        )
        return (
            self._require_job(job.handle),
            _delivery_label(delivery),
            ControlTransport.NATIVE_RUNTIME.value,
        )

    def _reject_provider_send_busy(self, participant_id: str, *, exclude: str | None) -> None:
        """Serialize provider sends behind accepted work and uncertain execution."""
        if self._store.has_execution_barrier(participant_id):
            raise Busy(
                f"participant {participant_id!r} has an unresolved delivery; "
                "reconcile it before sending another prompt"
            )
        queued = self._store.queued_control_operation_count(participant_id)
        if queued:
            raise Busy(
                f"participant {participant_id!r} has {queued} queued followup(s); "
                "an ordinary send cannot jump ahead of them"
            )
        participant = self._store.get_participant(participant_id)
        if participant is not None and participant.status is Status.WORKING:
            raise Busy(f"participant {participant_id!r} is working; not delivering now")
        running = self._store.active_running_jobs_for_target(participant_id)
        if any(job.handle != exclude and job.prompt for job in running):
            raise Busy(f"participant {participant_id!r} has a running send job")

    async def _snapshot_for_control(
        self, runtime: HarnessRuntime, participant_id: str, *, initial_dispatch: bool = False
    ) -> RuntimeSnapshot:
        """Refresh focus first, then read runtime state and recheck focus without yielding."""
        if not initial_dispatch:
            await self._gates.require_absent(participant_id)
        snapshot = await runtime.snapshot()
        if not initial_dispatch:
            self._gates.check_absent(participant_id)
        if runtime is not self._runtime_for(participant_id):
            raise StaleTarget(f"runtime for {participant_id!r} changed during control preparation")
        self._gates.record_native_snapshot(participant_id, runtime, snapshot)
        return snapshot

    def _require_current_native_route(
        self,
        participant_id: str,
        capability: RuntimeCapability,
        snapshot: RuntimeSnapshot,
        *,
        require_available: bool = True,
    ) -> ControlRoute:
        """Fence production native controls to the exact durable cached session."""
        route = self.route_for(participant_id, capability)
        if not route.is_native:
            raise StaleTarget(
                f"the native route for participant {participant_id!r} changed during preparation"
            )
        if not self._native_identity_fencing:
            return route
        native = route.native_route
        if native is None or (require_available and not route.route_available):
            raise StaleTarget(
                f"the exact native runtime route for participant {participant_id!r} is unavailable"
            )
        if (
            snapshot.native_session_id is None
            or native.get("backend_generation") != snapshot.backend_generation
            or native.get("native_session_id") != snapshot.native_session_id
        ):
            raise StaleTarget(
                f"the native runtime identity for participant {participant_id!r} changed "
                "during control preparation"
            )
        return route

    async def _send_legacy(
        self,
        participant_id: str,
        *,
        caller_id: str,
        prompt: str,
        response_format: str | None,
        operation_id: str | None = None,
        on_reserved: Callable[[str, str | None], None] | None = None,
        actor_client_id: str | None = None,
        actor_participant_id: str | None = None,
    ) -> Job:
        """Legacy transport: the same durable receipt transitions, no runtime."""
        await self._gates.require_absent(participant_id)
        await self._gates.legacy_copy_mode_check(participant_id)
        # Recheck after the awaited copy-mode query, before any durable effect.
        await self._gates.require_absent(participant_id)
        # Keep FIFO refusal ordering: a queued followup refuses the send before any job row or
        # typing, so it stays behind the queue; queue dispatch skips _send_legacy, so no self-block.
        queued = self._store.queued_control_operation_count(participant_id)
        if queued:
            raise Busy(
                f"participant {participant_id!r} has {queued} queued followup(s); "
                "an ordinary send cannot jump ahead of them — await the queued "
                "handles or queue another followup instead"
            )
        # The busy/claim check mutates claim rows; it runs after all awaited
        # prep, its synchronous body adjacent to reservation and delivery.
        participant = self._store.get_participant(participant_id)
        if participant is not None and participant.status is Status.WORKING:
            message = f"participant {participant_id!r} is working; not injecting a new prompt."
            if participant.parent_id == caller_id:
                message += (
                    f" Call interrupt_session(target={participant_id!r}), wait until "
                    "list_participants reports status='idle', then retry send."
                )
            else:
                message += (
                    " Wait until list_participants reports status='idle', then retry send; "
                    "only the participant's direct parent may interrupt it."
                )
            raise Busy(message)
        await self._gates.legacy_busy_check(participant_id)
        job = self._create_send_job(
            participant_id,
            caller_id=caller_id,
            prompt=prompt,
            response_format=response_format,
            actor_client_id=actor_client_id,
            actor_participant_id=actor_participant_id,
        )
        operation_id = operation_id or self._mint_operation_id(participant_id, ControlKind.SEND)
        self._reserve(
            operation_id,
            participant_id=participant_id,
            kind=ControlKind.SEND,
            transport=ControlTransport.LEGACY_TMUX,
            phase=ControlDeliveryPhase.RESERVED,
            job_handle=job.handle,
        )
        self._notify_reserved(on_reserved, operation_id, job.handle)
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
        self._record_legacy_send(participant_id, caller_id=caller_id, job=job, prompt=prompt)
        return self._require_job(job.handle)

    def _record_legacy_send(
        self,
        participant_id: str,
        *,
        caller_id: str,
        job: Job,
        prompt: str,
    ) -> None:
        self._store.bus_append(
            "agent.send",
            from_id=caller_id,
            to_id=participant_id,
            payload={"handle": job.handle, "prompt": prompt[:200]},
        )

    def _disconnected_native_refusal(self, participant_id: str, control: str) -> StaleTarget:
        """A persisted native binding whose runtime is gone: fail closed."""
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
        actor_client_id: str | None = None,
        actor_participant_id: str | None = None,
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
            actor_client_id=actor_client_id,
            actor_participant_id=actor_participant_id,
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
        """Validate a job handle for native initial-dispatch reuse."""
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
            # The initial dispatch happens exactly once.
            raise BadRequest(
                f"job {job_handle!r} already carries a control operation; the "
                f"initial dispatch of participant {participant_id!r} happens "
                "exactly once and is never retransmitted"
            )
        return job
