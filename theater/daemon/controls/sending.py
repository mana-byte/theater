"""Ordinary send over native and provider routes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from theater.constants.daemon import SEND_SUPERSEDED_ERROR_CODE
from theater.constants.refusals import NEXT_SELF_SEND, NEXT_SEND_BUSY
from theater.daemon.controls._common import (
    ACTION_SEND,
    CONTROL_DELIVERY_QUEUED,
    NativeControlPreparation,
    _delivery_label,
    prepare_native_control,
)
from theater.daemon.controls._host import ControlHost
from theater.daemon.controls.busy import BusyOperation
from theater.daemon.controls.provider_delivery import ProviderPresenceDeferred
from theater.daemon.controls.routing import ControlRoute
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    HarnessRuntime,
    RuntimeCapability,
    RuntimeSnapshot,
)
from theater.models import (
    BadRequest,
    Busy,
    Job,
    JobState,
    NotAddressable,
    StaleTarget,
    Status,
)


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
    defer_provider_presence: bool


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
        defer_provider_presence: bool = False,
    ) -> Job:
        """Ordinary send — or the native initial dispatch of one spawn job."""
        with self._control_latency(ControlKind.SEND, participant_id) as latency:
            try:
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
                        defer_provider_presence,
                    )
                )
            except Busy as exc:
                if caller_id == participant_id:
                    raise Busy(
                        f"participant {participant_id!r} is busy; a self-send cannot be queued",
                        details={"next": NEXT_SELF_SEND},
                    ) from exc
                raise
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
            if self._store.repair_fence_active(participant_id):
                raise Busy(
                    f"participant {participant_id!r} has an unresolved internal delivery; it "
                    "clears when that turn's evidence arrives or its backend restarts",
                    details={"next": "wait for the internal delivery to settle, then retry send"},
                )
            await self._require_absent(participant_id, route)
        self._gates.check_prompt(request.prompt)
        await self._gates.send_preflight(participant_id)
        return route, initial_dispatch

    async def _send_provider(
        self, request: _SendRequest, route: ControlRoute, *, initial_dispatch: bool
    ) -> tuple[Job, str, str]:
        participant_id = request.participant_id
        modal_check = self._gates.provider_modal_check
        if modal_check is not None:
            # Before any job or reservation: a modal refusal leaves nothing behind.
            await modal_check(participant_id)
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
            self._admit_provider_send(participant_id)
            self._reject_provider_send_busy(participant_id, exclude=None)
            # The route and presence can still change during the awaits above. Both are rechecked
            # here with no await before delivery re-checks them, so a refusal never strands a job.
            self._provider.require(participant_id, RuntimeCapability.SEND, route)
            self._gates.check_absent(participant_id)
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
        try:
            provider_delivery = await self._provider.deliver(
                route,
                capability=RuntimeCapability.SEND,
                kind=ControlKind.SEND,
                participant_id=participant_id,
                control_operation_id=control_id,
                callback_operation_id=request.callback_operation_id or control_id,
                action={"kind": "submit_text", "text": request.prompt},
                job_handle=provider_job.handle,
                defer_on_presence=request.defer_provider_presence,
            )
        except ProviderPresenceDeferred as exc:
            self._provider.defer_presence(control_id, error=str(exc))
            self._jobs.detach_touch_accumulator(provider_job.handle)
            self.schedule_dispatch(participant_id)
            return (
                self._require_job(provider_job.handle),
                CONTROL_DELIVERY_QUEUED,
                ControlTransport.PROVIDER_TERMINAL.value,
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

    def _admit_provider_send(self, participant_id: str) -> None:
        """Expire old prompt claims only after checking WORKING and before fresh-job refusal."""
        participant = self._store.get_participant(participant_id)
        if participant is not None and participant.status is Status.WORKING:
            raise Busy(
                f"participant {participant_id!r} is working; not delivering now",
                details={"next": NEXT_SEND_BUSY},
            )
        stale = self._gates.send_claim_now() - self._gates.send_claim_ttl()
        running_prompt_jobs = [
            job for job in self._store.active_running_jobs_for_target(participant_id) if job.prompt
        ]
        for job in (item for item in running_prompt_jobs if item.created_at <= stale):
            self._jobs.finish(
                job.handle,
                state=JobState.CRASHED,
                result=(
                    f"Send to participant {participant_id!r} was superseded after its "
                    "delivery claim expired; await the newer send handle instead."
                ),
                error_code=SEND_SUPERSEDED_ERROR_CODE,
            )
        if any(job.created_at > stale for job in running_prompt_jobs):
            raise Busy(
                f"participant {participant_id!r} has a running send job",
                details={"next": NEXT_SEND_BUSY},
            )

    def _reject_provider_send_busy(self, participant_id: str, *, exclude: str | None) -> None:
        """Serialize provider sends behind accepted work and uncertain execution."""
        if self._store.has_execution_barrier(participant_id):
            raise Busy(
                f"participant {participant_id!r} has an unresolved delivery; "
                "reconcile it before sending another prompt",
                details={"next": "wait for the unresolved delivery to settle, then retry send"},
            )
        queued = self._store.queued_control_operation_count(participant_id)
        if queued:
            raise Busy(
                f"participant {participant_id!r} has {queued} queued followup(s); "
                "an ordinary send cannot jump ahead of them",
                details={"next": NEXT_SEND_BUSY},
            )
        participant = self._store.get_participant(participant_id)
        if participant is not None and participant.status is Status.WORKING:
            raise Busy(
                f"participant {participant_id!r} is working; not delivering now",
                details={"next": NEXT_SEND_BUSY},
            )
        running = self._store.active_running_jobs_for_target(participant_id)
        if any(job.handle != exclude and job.prompt for job in running):
            raise Busy(
                f"participant {participant_id!r} has a running send job",
                details={"next": NEXT_SEND_BUSY},
            )

    async def _snapshot_for_control(
        self,
        runtime: HarnessRuntime,
        participant_id: str,
        *,
        initial_dispatch: bool = False,
        defer_presence: bool = False,
    ) -> RuntimeSnapshot:
        """Refresh focus first, then read runtime state and recheck focus without yielding."""
        gated = not (initial_dispatch or defer_presence)
        if gated:
            await self._gates.require_absent(participant_id)
        snapshot = await runtime.snapshot()
        if gated:
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
