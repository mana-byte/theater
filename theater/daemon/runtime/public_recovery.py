"""Deterministic recovery for public work proven never dispatched."""

from __future__ import annotations

import math
from dataclasses import replace

from sqlalchemy import update

from theater.daemon.operations.projection import operation_event_payload
from theater.daemon.operations.service import IDEMPOTENCY_RETENTION_SECONDS
from theater.daemon.schema import launch_reservations
from theater.harness.contracts.runtime import ControlDeliveryPhase
from theater.models import (
    Job,
    JobState,
    JournalEventRecord,
    Participant,
    ParticipantOrigin,
    PublicOperationRecord,
    PublicOperationState,
    Status,
    WorkspaceUsageHolderKind,
    WorkspaceUsageRecord,
    now,
)

_RESTART_ERROR = {
    "code": "daemon_restarted",
    "message": "the daemon restarted before provider dispatch; the request was not replayed",
}


def fail_proven_undispatched(daemon, operation: PublicOperationRecord) -> bool:
    """Atomically fail one operation proven not to have dispatched a mutation."""
    timestamp = math.nextafter(max(now(), operation.updated_at), math.inf)
    with daemon.store.write_unit() as unit:
        current = daemon.store.operations.get(operation.operation_id, connection=unit.connection)
        if current is None or current.state not in {
            PublicOperationState.ACCEPTED.value,
            PublicOperationState.RUNNING.value,
        }:
            return False
        if _has_possible_dispatch(daemon, current, connection=unit.connection):
            return False
        events: list[JournalEventRecord] = []
        finished_job: Job | None = None
        retired_participant: Participant | None = None
        if current.kind == "spawn":
            retired_participant, finished_job = _rollback_accepted_spawn(
                daemon, current, timestamp=timestamp, unit=unit, events=events
            )
        elif current.kind == "adopt":
            retired_participant = _rollback_accepted_adoption(
                daemon, current, timestamp=timestamp, unit=unit, events=events
            )

        updated = replace(
            current,
            state=PublicOperationState.FAILED.value,
            phase="daemon_restart_before_dispatch",
            error=dict(_RESTART_ERROR),
            error_code=str(_RESTART_ERROR["code"]),
            result=None,
            dispatch_provider_id=None,
            dispatch_provider_generation=None,
            dispatch_terminal_id=None,
            dispatch_terminal_incarnation=None,
            dispatch_terminal_occupant_evidence=None,
            dispatch_terminal_process_facts=None,
            dispatch_backend_generation=None,
            dispatch_native_session_id=None,
            dispatch_native_turn_id=None,
            updated_at=timestamp,
            settled_at=timestamp,
        )
        if not daemon.store.operations.replace(
            updated,
            expected_state=current.state,
            expected_updated_at=current.updated_at,
            connection=unit.connection,
        ):
            raise RuntimeError("accepted operation changed during restart recovery")
        daemon.store.operations.settle_idempotency_for_operation(
            current.operation_id,
            settled_at=timestamp,
            retain_until=timestamp + IDEMPOTENCY_RETENTION_SECONDS,
            connection=unit.connection,
        )
        events.append(
            JournalEventRecord(
                kind="operation.updated",
                entity_id=updated.operation_id,
                entity_revision=0,
                payload=operation_event_payload(updated),
                recorded_at=timestamp,
            )
        )
        first = daemon.store.journal.current_sequence(connection=unit.connection) + 1
        daemon.store.journal.append_group(
            unit,
            [replace(event, entity_revision=first + index) for index, event in enumerate(events)],
        )
        unit.after_commit(
            lambda operation_id=current.operation_id: (
                daemon.operation_service.notify_persisted_change(operation_id)
            )
        )
        if finished_job is not None:
            unit.after_commit(
                lambda handle=finished_job.handle: daemon.jobs.finish(
                    handle,
                    state=JobState.CRASHED,
                    error_code="daemon_restarted",
                )
            )
        if retired_participant is not None:
            unit.after_commit(
                lambda participant_id=retired_participant.id: daemon.registry.mark_dead(
                    participant_id
                )
            )
    return True


def _has_possible_dispatch(daemon, operation: PublicOperationRecord, *, connection) -> bool:
    if operation.kind == "spawn":
        launch = daemon.store.operations.get_launch(operation.operation_id, connection=connection)
        return launch is None or launch.dispatch_marker is not None
    control_ids = [operation.control_operation_id]
    if operation.kind.startswith("controls."):
        control_ids.append(f"{operation.operation_id}:control")
    for control_id in control_ids:
        if control_id is None:
            continue
        control = daemon.store.get_control_operation(control_id, connection=connection)
        if control is not None:
            return control.delivery_phase not in {
                ControlDeliveryPhase.RESERVED,
                ControlDeliveryPhase.QUEUED,
            }
    if operation.kind.startswith("controls."):
        return False
    return any(
        value is not None
        for value in (
            operation.dispatch_provider_id,
            operation.dispatch_provider_generation,
            operation.dispatch_terminal_id,
            operation.dispatch_terminal_incarnation,
            operation.dispatch_backend_generation,
            operation.dispatch_native_session_id,
            operation.dispatch_native_turn_id,
        )
    )


def _rollback_accepted_spawn(
    daemon,
    operation: PublicOperationRecord,
    *,
    timestamp: float,
    unit,
    events: list[JournalEventRecord],
) -> tuple[Participant | None, Job | None]:
    launch = daemon.store.operations.get_launch(operation.operation_id, connection=unit.connection)
    if launch is None or launch.dispatch_marker is not None:
        return None, None
    participant = daemon.store.get_participant(launch.participant_id, connection=unit.connection)
    retired = _retire_reserved_participant(
        daemon, participant, timestamp=timestamp, unit=unit, events=events
    )
    finished = _finish_reserved_job(
        daemon, operation.job_handle, timestamp=timestamp, unit=unit, events=events
    )
    if launch.workspace_usage_id is not None:
        usage = daemon.store.workspaces.get_usage(
            launch.workspace_usage_id, connection=unit.connection
        )
        if (
            usage is not None
            and usage.released_at is None
            and usage.holder_kind == WorkspaceUsageHolderKind.RESERVATION.value
            and usage.holder_id == operation.operation_id
            and daemon.store.workspaces.release_usage(
                usage.usage_id,
                released_at=timestamp,
                reason="daemon_restarted",
                connection=unit.connection,
            )
        ):
            events.append(_usage_event(replace(usage, released_at=timestamp), timestamp))
    unit.connection.execute(
        update(launch_reservations)
        .where(launch_reservations.c.operation_id == operation.operation_id)
        .values(phase="rolled_back", updated_at=timestamp)
    )
    return retired, finished


def _rollback_accepted_adoption(
    daemon,
    operation: PublicOperationRecord,
    *,
    timestamp: float,
    unit,
    events: list[JournalEventRecord],
) -> Participant | None:
    if len(operation.target_ids) != 1:
        return None
    participant = daemon.store.get_participant(operation.target_ids[0], connection=unit.connection)
    if participant is None or participant.origin is not ParticipantOrigin.ADOPTED:
        return None
    if daemon.store.terminal_bindings.get(participant.id, connection=unit.connection) is not None:
        return None
    return _retire_reserved_participant(
        daemon, participant, timestamp=timestamp, unit=unit, events=events
    )


def _retire_reserved_participant(
    daemon,
    participant: Participant | None,
    *,
    timestamp: float,
    unit,
    events: list[JournalEventRecord],
) -> Participant | None:
    if participant is None or participant.status is Status.DEAD:
        return None
    retired = replace(
        participant,
        status=Status.DEAD,
        termination_reason="daemon_restarted",
        terminated_at=timestamp,
        last_activity=timestamp,
    )
    daemon.store.upsert_participant(retired, connection=unit.connection)
    events.append(
        JournalEventRecord(
            kind="participant.updated",
            entity_id=retired.id,
            entity_revision=0,
            payload={
                "participant_id": retired.id,
                "status": Status.DEAD.value,
                "parent_id": retired.parent_id,
                "workspace_id": retired.workspace_id,
            },
            recorded_at=timestamp,
        )
    )
    return retired


def _finish_reserved_job(
    daemon,
    handle: str | None,
    *,
    timestamp: float,
    unit,
    events: list[JournalEventRecord],
) -> Job | None:
    if handle is None:
        return None
    job = daemon.store.get_job(handle, connection=unit.connection)
    if job is None or job.state != JobState.RUNNING:
        return None
    finished = replace(
        job,
        state=JobState.CRASHED.value,
        result="the daemon restarted before launch dispatch",
        error_code="daemon_restarted",
        finished_at=timestamp,
    )
    daemon.store.finish_job(
        handle,
        state=finished.state,
        result=finished.result,
        error_code=finished.error_code,
        finished_at=timestamp,
        response_format=finished.response_format,
        structured_result=finished.structured_result,
        structured_status=finished.structured_status,
        connection=unit.connection,
    )
    events.append(
        JournalEventRecord(
            kind="job.updated",
            entity_id=finished.handle,
            entity_revision=0,
            payload={
                "handle": finished.handle,
                "state": str(finished.state),
                "kind": str(finished.kind),
                "target_id": finished.target_id,
                "error": {
                    "code": "daemon_restarted",
                    "message": finished.result or "",
                },
            },
            recorded_at=timestamp,
        )
    )
    return finished


def _usage_event(usage: WorkspaceUsageRecord, timestamp: float) -> JournalEventRecord:
    return JournalEventRecord(
        kind="workspace.usage_changed",
        entity_id=usage.workspace_id,
        entity_revision=0,
        payload={
            "workspace_id": usage.workspace_id,
            "usage_id": usage.usage_id,
            "holder_kind": usage.holder_kind,
            "holder_id": usage.holder_id,
            "acquired_at": usage.acquired_at,
            "action": "released",
        },
        recorded_at=timestamp,
    )


__all__ = ["fail_proven_undispatched"]
