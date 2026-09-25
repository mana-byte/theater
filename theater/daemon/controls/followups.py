"""Queued followups: admission and predecessor binding of queued prompts."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from theater.constants.daemon import CONTROL_QUEUE_MAX_PENDING
from theater.daemon.controls._common import (
    ACTION_QUEUE_FOLLOWUP,
    ACTION_SEND,
    CONTROL_DELIVERY_QUEUED,
    NativeControlPreparation,
    prepare_native_control,
)
from theater.daemon.controls._host import ControlHost
from theater.daemon.controls.routing import ControlRoute
from theater.daemon.events.publication import control_event, next_revision
from theater.daemon.persistence.repositories.control_operations import ControlOperation
from theater.harness.contracts.runtime import (
    ConnectionHealth,
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    RuntimeCapability,
    RuntimeSnapshot,
)
from theater.models import BadRequest, Busy, Job, JobState


@dataclass(frozen=True, slots=True)
class _FollowupRequest:
    participant_id: str
    caller_id: str
    prompt: str
    response_format: str | None
    operation_id: str | None
    callback_operation_id: str | None
    on_reserved: Callable[[str, str | None], None] | None
    actor_client_id: str | None
    actor_participant_id: str | None
    pre_reserved: bool


@dataclass(frozen=True, slots=True)
class _FollowupRoute:
    route: ControlRoute
    snapshot: RuntimeSnapshot | None = None
    generation: int | None = None
    session: str | None = None
    predecessor: str | None = None


class FollowupControls(ControlHost):
    """Queue prompts for a participant's next idle moment."""

    async def queue_followup(
        self,
        participant_id: str,
        *,
        caller_id: str,
        prompt: str,
        response_format: str | None = None,
        operation_id: str | None = None,
        callback_operation_id: str | None = None,
        on_reserved: Callable[[str, str | None], None] | None = None,
        actor_client_id: str | None = None,
        actor_participant_id: str | None = None,
        pre_reserved: bool = False,
    ) -> Job:
        """Create an awaitable send job immediately and reserve its queue slot."""
        with self._control_latency(ControlKind.QUEUE_FOLLOWUP, participant_id) as latency:
            job, transport = await self._queue_followup(
                _FollowupRequest(
                    participant_id,
                    caller_id,
                    prompt,
                    response_format,
                    operation_id,
                    callback_operation_id,
                    on_reserved,
                    actor_client_id,
                    actor_participant_id,
                    pre_reserved,
                )
            )
            # The queue accepted the item; its delivery is observed when a
            # dispatch pass delivers it, never optimistically here.
            latency.delivery = CONTROL_DELIVERY_QUEUED
            latency.transport = transport
            return job

    async def _queue_followup(self, request: _FollowupRequest) -> tuple[Job, str]:
        """The queue-followup body; returns its job and the reserved transport."""
        participant_id = request.participant_id
        async with self._lock(participant_id):
            self._gates.authorize(participant_id, request.caller_id, ACTION_QUEUE_FOLLOWUP)
            if (existing := self._settled_followup(request)) is not None:
                return existing
            await self._gates.require_absent(participant_id)
            self._gates.check_prompt(request.prompt)
            self._require_followup_capacity(participant_id, pre_reserved=request.pre_reserved)
            prepared = await self._prepare_followup_route(participant_id)
            self._gates.check_absent(participant_id)
            if request.pre_reserved:
                return self._reuse_followup(request, prepared)
            job, transport = self._reserve_followup(request, prepared)
        # Already idle? Dispatch on the next scheduling opportunity.
        self.schedule_dispatch(participant_id)
        return job, transport.value

    def _settled_followup(self, request: _FollowupRequest) -> tuple[Job, str] | None:
        if not request.pre_reserved:
            return None
        if request.operation_id is None:
            raise RuntimeError("a pre-reserved public followup requires its control ID")
        existing = self._store.get_control_operation(request.operation_id)
        if (
            existing is not None
            and existing.delivery_phase is ControlDeliveryPhase.SETTLED
            and existing.job_handle is not None
        ):
            return self._require_job(existing.job_handle), existing.transport.value
        return None

    def _require_followup_capacity(self, participant_id: str, *, pre_reserved: bool) -> None:
        pending = self._store.queued_control_operation_count(participant_id)
        queue_full = (
            pending > CONTROL_QUEUE_MAX_PENDING
            if pre_reserved
            else pending >= CONTROL_QUEUE_MAX_PENDING
        )
        if queue_full:
            raise Busy(
                f"participant {participant_id!r} already holds {pending} queued "
                f"followups (bound {CONTROL_QUEUE_MAX_PENDING}); await or "
                "interrupt the pending handles before queueing another"
            )

    async def _prepare_followup_route(self, participant_id: str) -> _FollowupRoute:
        route = self.route_for(participant_id, RuntimeCapability.QUEUE_FOLLOWUP)
        if route.transport is None:
            raise BadRequest(f"participant {participant_id!r} does not offer a followup transport")
        runtime = self._runtime_for(participant_id)
        if route.is_provider:
            self._provider.require(participant_id, RuntimeCapability.QUEUE_FOLLOWUP, route)
            return _FollowupRoute(route)
        if not route.is_native:
            return _FollowupRoute(route)
        prepared = await prepare_native_control(
            self,
            runtime,
            NativeControlPreparation(
                participant_id=participant_id,
                route_capability=RuntimeCapability.QUEUE_FOLLOWUP,
                required_capability=RuntimeCapability.SEND,
                action=ACTION_SEND,
                refusal_label=ACTION_QUEUE_FOLLOWUP,
                require_available=False,
            ),
        )
        snapshot = prepared.snapshot
        return _FollowupRoute(
            prepared.route,
            snapshot,
            snapshot.backend_generation,
            snapshot.native_session_id,
            self._queue_predecessor(participant_id, snapshot),
        )

    def _reuse_followup(
        self, request: _FollowupRequest, prepared: _FollowupRoute
    ) -> tuple[Job, str]:
        operation_id = request.operation_id
        if operation_id is None:
            raise RuntimeError("a pre-reserved public followup requires its control ID")
        reserved = self._require_public_reservation(
            operation_id,
            participant_id=request.participant_id,
            kind=ControlKind.QUEUE_FOLLOWUP,
            phase=ControlDeliveryPhase.QUEUED,
            route=prepared.route,
        )
        if reserved.job_handle is None:
            raise RuntimeError("a queued public control requires its durable job")
        if prepared.snapshot is not None:
            self._require_reserved_native_identity(reserved, prepared.snapshot)
            payload = self._queue_payload(
                predecessor=prepared.predecessor,
                callback_operation_id=request.callback_operation_id,
            )
            self._store.set_queued_control_payload(operation_id, payload or "{}")
        job = self._require_job(reserved.job_handle)
        self.schedule_dispatch(request.participant_id)
        return job, reserved.transport.value

    def _reserve_followup(
        self, request: _FollowupRequest, prepared: _FollowupRoute
    ) -> tuple[Job, ControlTransport]:
        participant_id = request.participant_id
        with self._store.write_unit() as unit:
            connection = unit.connection
            sequence = self._store.allocate_control_queue_sequence(connection=connection)
            handle = f"{participant_id}#{sequence}"
            assert prepared.route.transport is not None
            transport = prepared.route.transport
            control_id = request.operation_id or f"{handle}:{ControlKind.QUEUE_FOLLOWUP.value}"
            payload = self._queue_payload(
                predecessor=prepared.predecessor,
                callback_operation_id=request.callback_operation_id,
            )
            if prepared.route.is_provider:
                self._provider.reserve(
                    control_id,
                    prepared.route,
                    participant_id=participant_id,
                    kind=ControlKind.QUEUE_FOLLOWUP,
                    phase=ControlDeliveryPhase.QUEUED,
                    job_handle=handle,
                    queue_sequence=sequence,
                    payload=payload,
                    connection=connection,
                )
            else:
                self._reserve(
                    control_id,
                    participant_id=participant_id,
                    kind=ControlKind.QUEUE_FOLLOWUP,
                    transport=transport,
                    phase=ControlDeliveryPhase.QUEUED,
                    job_handle=handle,
                    backend_generation=prepared.generation,
                    native_session_id=prepared.session,
                    queue_sequence=sequence,
                    payload=payload,
                    connection=connection,
                )
            reserved = self._store.get_control_operation(control_id, connection=connection)
            assert reserved is not None
            event = control_event(
                self._store,
                reserved,
                connection,
                revision=next_revision(self._store, connection),
            )
            if event is not None:
                self._store.journal.append_group(unit, [event])
        self._jobs.create(
            handle=handle,
            caller_id=request.caller_id,
            target_id=participant_id,
            kind="send",
            prompt=request.prompt,
            cwd=None,
            response_format=request.response_format,
            actor_client_id=request.actor_client_id,
            actor_participant_id=request.actor_participant_id,
        )
        self._notify_reserved(request.on_reserved, control_id, handle)
        return self._require_job(handle), transport

    def _queue_predecessor(self, participant_id: str, snapshot: RuntimeSnapshot) -> str | None:
        """Capture exact execution context, never a stale completed job."""
        turn = snapshot.native_turn_id
        session = snapshot.native_session_id
        if (
            turn is None
            or session is None
            or snapshot.health not in (ConnectionHealth.CONNECTED, ConnectionHealth.DEGRADED)
        ):
            return None
        if (
            self._store.get_native_terminal_evidence(
                participant_id=participant_id,
                backend_generation=snapshot.backend_generation,
                native_session_id=session,
                native_turn_id=turn,
            )
            is not None
        ):
            return None
        operation = self._operation_for_turn(
            participant_id=participant_id,
            backend_generation=snapshot.backend_generation,
            native_session_id=session,
            native_turn_id=turn,
        )
        if operation is not None and operation.job_handle is not None:
            job = self._store.get_job(operation.job_handle)
            if job is None or job.state != JobState.RUNNING:
                return None
        return turn

    @staticmethod
    def _queue_payload(*, predecessor: str | None, callback_operation_id: str | None) -> str | None:
        values = {
            key: value
            for key, value in (
                ("queue_predecessor_turn", predecessor),
                ("callback_operation_id", callback_operation_id),
            )
            if value is not None
        }
        return json.dumps(values) if values else None

    @staticmethod
    def _callback_operation_id(operation: ControlOperation) -> str | None:
        if operation.payload is None:
            return None
        try:
            value = json.loads(operation.payload)
        except (TypeError, ValueError):
            return None
        callback_id = value.get("callback_operation_id") if isinstance(value, dict) else None
        return callback_id if isinstance(callback_id, str) and callback_id else None

    def _bind_queued_predecessor(
        self,
        participant_id: str,
        snapshot: RuntimeSnapshot,
        turn: str,
        *,
        connection=None,
    ) -> None:
        """Move the bounded pending FIFO behind an actually observed/accepted turn."""
        for operation in self._store.queued_control_operations(
            participant_id, connection=connection
        ):
            payload = self._queue_payload(
                predecessor=turn,
                callback_operation_id=self._callback_operation_id(operation),
            )
            assert payload is not None
            if (
                operation.transport is ControlTransport.NATIVE_RUNTIME
                and operation.backend_generation == snapshot.backend_generation
                and operation.native_session_id == snapshot.native_session_id
                and operation.payload != payload
            ):
                self._store.set_queued_control_payload(
                    operation.operation_id, payload, connection=connection
                )
