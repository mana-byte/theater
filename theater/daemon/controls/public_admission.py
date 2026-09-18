"""Atomic durable admission for public control operations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import partial

from theater.daemon.controls.routing import ControlRoute
from theater.daemon.events.publication import control_event, job_event, next_revision
from theater.daemon.persistence.repositories.control_operations import ControlOperation
from theater.daemon.persistence.transactions import WriteUnit
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    RuntimeWiring,
)
from theater.models import BadRequest, Job, JobState, JournalEventRecord, StaleTarget


@dataclass(frozen=True, slots=True)
class PublicControlReservation:
    control_operation_id: str
    job_handle: str | None
    events: tuple[JournalEventRecord, ...]


class PublicControlAdmission:
    """Persist a public job and control row inside the operation write unit."""

    def __init__(self, store, *, clock) -> None:
        self._store = store
        self._clock = clock

    def reserve(
        self,
        unit: WriteUnit,
        *,
        operation_id: str,
        participant_id: str,
        kind: ControlKind,
        route: ControlRoute,
        caller_id: str,
        actor_client_id: str,
        prompt: str | None = None,
        response_format: str | None = None,
        expected_turn_id: str | None = None,
        settings: dict[str, str] | None = None,
    ) -> PublicControlReservation:
        transport = route.transport
        if transport is None:
            raise BadRequest(
                f"participant {participant_id!r} does not offer a transport for {kind.value}"
            )
        connection = unit.connection
        timestamp = self._clock()
        control_id = f"{operation_id}:control"
        sequence, job = self._job(
            unit,
            participant_id=participant_id,
            kind=kind,
            caller_id=caller_id,
            actor_client_id=actor_client_id,
            prompt=prompt,
            response_format=response_format,
            timestamp=timestamp,
        )
        (
            backend_generation,
            native_session_id,
            native_turn_id,
            provider_id,
            provider_generation,
            terminal_id,
            terminal_incarnation,
        ) = self._identity(
            route,
            participant_id=participant_id,
            kind=kind,
            expected_turn_id=expected_turn_id,
            connection=connection,
        )

        payload = self._payload(
            kind,
            prompt=prompt,
            operation_id=operation_id,
            settings=settings,
        )
        control = ControlOperation(
            operation_id=control_id,
            participant_id=participant_id,
            kind=kind,
            transport=transport,
            delivery_phase=(
                ControlDeliveryPhase.QUEUED
                if kind is ControlKind.QUEUE_FOLLOWUP
                else ControlDeliveryPhase.RESERVED
            ),
            job_handle=None if job is None else job.handle,
            backend_generation=backend_generation,
            native_session_id=native_session_id,
            native_turn_id=native_turn_id,
            provider_id=provider_id,
            provider_generation=provider_generation,
            terminal_id=terminal_id,
            terminal_incarnation=terminal_incarnation,
            queue_sequence=sequence if kind is ControlKind.QUEUE_FOLLOWUP else None,
            payload=payload,
            created_at=timestamp,
            updated_at=timestamp,
        )
        self._store.reserve_control_operation(control, connection=connection)
        events: list[JournalEventRecord] = []
        revision = next_revision(self._store, connection)
        if job is not None and kind in {ControlKind.SEND, ControlKind.QUEUE_FOLLOWUP}:
            events.append(job_event(job, revision=revision, recorded_at=timestamp))
            revision += 1
        projected = control_event(
            self._store,
            control,
            connection,
            revision=revision,
        )
        if projected is not None:
            events.append(projected)
        return PublicControlReservation(
            control_id,
            None if job is None else job.handle,
            tuple(events),
        )

    def _job(
        self,
        unit: WriteUnit,
        *,
        participant_id: str,
        kind: ControlKind,
        caller_id: str,
        actor_client_id: str,
        prompt: str | None,
        response_format: str | None,
        timestamp: float,
    ) -> tuple[int | None, Job | None]:
        if kind is ControlKind.STEER:
            jobs = self._store.active_running_jobs_for_target(
                participant_id, connection=unit.connection
            )
            if len(jobs) != 1:
                raise StaleTarget(
                    f"participant {participant_id!r} does not have exactly one active "
                    "Theater job to steer"
                )
            return None, jobs[0]
        if kind not in {ControlKind.SEND, ControlKind.QUEUE_FOLLOWUP}:
            return None, None
        sequence = self._store.allocate_control_queue_sequence(connection=unit.connection)
        job = Job(
            handle=f"{participant_id}#{sequence}",
            caller_id=caller_id,
            target_id=participant_id,
            kind="send",
            prompt=prompt,
            state=JobState.RUNNING.value,
            result=None,
            error_code=None,
            created_at=timestamp,
            finished_at=None,
            response_format=response_format,
            actor_client_id=actor_client_id,
        )
        self._store.create_job(job, connection=unit.connection)
        unit.after_commit(partial(self._publish_job_created, job))
        return sequence, job

    def _identity(
        self,
        route: ControlRoute,
        *,
        participant_id: str,
        kind: ControlKind,
        expected_turn_id: str | None,
        connection,
    ) -> tuple[int | None, str | None, str | None, str | None, int | None, str | None, str | None]:
        if route.transport is ControlTransport.PROVIDER_TERMINAL:
            terminal = route.terminal
            if terminal is None:
                raise StaleTarget("provider control route has no terminal binding")
            if not route.route_available:
                raise StaleTarget(
                    f"provider terminal route for participant {participant_id!r} is unavailable"
                )
            return (
                None,
                None,
                None,
                terminal.provider_id,
                terminal.provider_generation,
                terminal.terminal_id,
                terminal.terminal_incarnation,
            )
        if route.transport is ControlTransport.NATIVE_RUNTIME:
            if not route.route_available:
                raise StaleTarget(
                    f"native runtime route for participant {participant_id!r} is unavailable"
                )
            binding = self._store.get_runtime_binding(participant_id, connection=connection)
            if binding is not None and binding.wiring is RuntimeWiring.NATIVE:
                return (
                    binding.backend_generation,
                    binding.native_session_id,
                    expected_turn_id if kind is ControlKind.STEER else None,
                    None,
                    None,
                    None,
                    None,
                )
        return (None, None, None, None, None, None, None)

    def _publish_job_created(self, job: Job) -> None:
        self._store.bus_append(
            "job.created",
            from_id=job.caller_id,
            to_id=job.target_id,
            payload={"handle": job.handle, "kind": str(job.kind)},
        )

    @staticmethod
    def _payload(
        kind: ControlKind,
        *,
        prompt: str | None,
        operation_id: str,
        settings: dict[str, str] | None,
    ) -> str | None:
        if kind is ControlKind.QUEUE_FOLLOWUP:
            return json.dumps({"callback_operation_id": operation_id})
        if kind is ControlKind.STEER:
            return prompt
        if kind is ControlKind.SETTINGS_UPDATE:
            return json.dumps(settings or {})
        return None


__all__ = ["PublicControlAdmission", "PublicControlReservation"]
