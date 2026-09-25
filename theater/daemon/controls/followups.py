"""Queued followups: admission and predecessor binding of queued prompts."""

from __future__ import annotations

import json
from collections.abc import Callable

from theater.constants.daemon import CONTROL_QUEUE_MAX_PENDING
from theater.daemon.controls._common import ACTION_QUEUE_FOLLOWUP, CONTROL_DELIVERY_QUEUED
from theater.daemon.controls._host import ControlHost
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
                participant_id,
                caller_id=caller_id,
                prompt=prompt,
                response_format=response_format,
                operation_id=operation_id,
                callback_operation_id=callback_operation_id,
                on_reserved=on_reserved,
                actor_client_id=actor_client_id,
                actor_participant_id=actor_participant_id,
                pre_reserved=pre_reserved,
            )
            # The queue accepted the item; its delivery is observed when a
            # dispatch pass delivers it, never optimistically here.
            latency.delivery = CONTROL_DELIVERY_QUEUED
            latency.transport = transport
            return job

    async def _queue_followup(  # noqa: PLR0912, PLR0915
        self,
        participant_id: str,
        *,
        caller_id: str,
        prompt: str,
        response_format: str | None,
        operation_id: str | None,
        callback_operation_id: str | None,
        on_reserved: Callable[[str, str | None], None] | None,
        actor_client_id: str | None,
        actor_participant_id: str | None,
        pre_reserved: bool,
    ) -> tuple[Job, str]:
        """The queue-followup body; returns its job and the reserved transport."""
        async with self._lock(participant_id):
            self._gates.authorize(participant_id, caller_id, ACTION_QUEUE_FOLLOWUP)
            if pre_reserved:
                if operation_id is None:
                    raise RuntimeError("a pre-reserved public followup requires its control ID")
                existing = self._store.get_control_operation(operation_id)
                if (
                    existing is not None
                    and existing.delivery_phase is ControlDeliveryPhase.SETTLED
                    and existing.job_handle is not None
                ):
                    return self._require_job(existing.job_handle), existing.transport.value
            await self._gates.require_absent(participant_id)
            self._gates.check_prompt(prompt)
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
            route = self.route_for(participant_id, RuntimeCapability.QUEUE_FOLLOWUP)
            if route.transport is None:
                raise BadRequest(
                    f"participant {participant_id!r} does not offer a followup transport"
                )
            # Pin native queued work to its reserved generation/session; never replay across backend
            # relaunches. Legacy queue entries deliberately carry no live-runtime identity.
            runtime = self._runtime_for(participant_id)
            generation: int | None = None
            session: str | None = None
            predecessor: str | None = None
            if route.is_provider:
                self._provider.require(participant_id, RuntimeCapability.QUEUE_FOLLOWUP, route)
            elif route.is_native and runtime is not None:
                snapshot = await self._snapshot_for_control(runtime, participant_id)
                route = self._require_current_native_route(
                    participant_id,
                    RuntimeCapability.QUEUE_FOLLOWUP,
                    snapshot,
                    require_available=False,
                )
                # The queue is Theater-owned, so the QUEUE_FOLLOWUP capability (forbidden native
                # thread/queue use) never gates it.
                self._require_capability(participant_id, snapshot, RuntimeCapability.SEND, "send")
                generation = snapshot.backend_generation
                session = snapshot.native_session_id
                predecessor = self._queue_predecessor(participant_id, snapshot)
            elif route.is_native:
                raise self._disconnected_native_refusal(participant_id, "queue_followup")
            self._gates.check_absent(participant_id)
            if pre_reserved:
                if operation_id is None:
                    raise RuntimeError("a pre-reserved public followup requires its control ID")
                reserved = self._require_public_reservation(
                    operation_id,
                    participant_id=participant_id,
                    kind=ControlKind.QUEUE_FOLLOWUP,
                    phase=ControlDeliveryPhase.QUEUED,
                    route=route,
                )
                if reserved.job_handle is None:
                    raise RuntimeError("a queued public control requires its durable job")
                if route.is_native and runtime is not None:
                    self._require_reserved_native_identity(reserved, snapshot)
                    payload = self._queue_payload(
                        predecessor=predecessor,
                        callback_operation_id=callback_operation_id,
                    )
                    self._store.set_queued_control_payload(operation_id, payload or "{}")
                job = self._require_job(reserved.job_handle)
                transport = reserved.transport
                self.schedule_dispatch(participant_id)
                return job, transport.value
            with self._store.write_unit() as unit:
                connection = unit.connection
                sequence = self._store.allocate_control_queue_sequence(connection=connection)
                handle = f"{participant_id}#{sequence}"
                assert route.transport is not None
                transport = route.transport
                control_id = operation_id or f"{handle}:{ControlKind.QUEUE_FOLLOWUP.value}"
                payload = self._queue_payload(
                    predecessor=predecessor,
                    callback_operation_id=callback_operation_id,
                )
                if route.is_provider:
                    self._provider.reserve(
                        control_id,
                        route,
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
                        backend_generation=generation,
                        native_session_id=session,
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
                caller_id=caller_id,
                target_id=participant_id,
                kind="send",
                prompt=prompt,
                cwd=None,
                response_format=response_format,
                actor_client_id=actor_client_id,
                actor_participant_id=actor_participant_id,
            )
            self._notify_reserved(on_reserved, control_id, handle)
            job = self._require_job(handle)
        # Already idle? Dispatch on the next scheduling opportunity.
        self.schedule_dispatch(participant_id)
        return job, transport.value

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
