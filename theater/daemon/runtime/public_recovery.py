"""Deterministic recovery for public work proven never dispatched."""

from __future__ import annotations

import logging
import math
from dataclasses import replace

from sqlalchemy import update

from theater.daemon.events.publication import participant_event, workspace_usage_event
from theater.daemon.operations import DispatchIntent, OperationOutcome
from theater.daemon.schema import launch_reservations
from theater.frontend.capabilities import MAX_ERROR_CODE_CHARS, MAX_ERROR_MESSAGE_CHARS
from theater.harness.contracts.runtime import ControlDeliveryPhase, DeliveryResult
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
    now,
)

logger = logging.getLogger(__name__)

_RESTART_ERROR = {
    "code": "daemon_restarted",
    "message": "the daemon restarted before provider dispatch; the request was not replayed",
}


async def reconcile_workspace_lifecycle(daemon) -> tuple[str, ...]:
    """Reconcile intent and cleanup rows without replaying any Git mutation."""
    service = getattr(daemon, "workspace_service", None)
    reconcile = getattr(service, "reconcile_retained_workspaces", None)
    recover_cleanup = getattr(service, "recover_cleanup_deletion", None)
    if not callable(reconcile) or not callable(recover_cleanup):
        return ()
    reconciled = list(await reconcile())
    release_dead = getattr(service, "reconcile_dead_holder_usages", None)
    if callable(release_dead):
        try:
            release_dead()
        except Exception:
            logger.exception("dead-holder usage reconcile failed at startup")
    cursor: tuple[float, str] | None = None
    while True:
        records, cursor = daemon.store.workspaces.list_by_states_page(
            ("deleting",), cursor=cursor, limit=100
        )
        for record in records:
            if record.ownership_kind != "theater" or record.deletion_operation_id is None:
                continue
            operation = daemon.store.operations.get(record.deletion_operation_id)
            if operation is None:
                continue
            if operation.kind == "workspace_cleanup":
                non_dispatch_proven = (
                    operation.state == PublicOperationState.ACCEPTED.value
                    and operation.phase == "workspace_cleanup_accepted"
                )
            elif operation.kind == "spawn" and record.cleanup_force is True:
                non_dispatch_proven = False
            else:
                continue
            state = await recover_cleanup(
                record.workspace_id,
                operation_id=record.deletion_operation_id,
                non_dispatch_proven=non_dispatch_proven,
            )
            if state is not None:
                reconciled.append(record.workspace_id)
        if cursor is None:
            break
    return tuple(reconciled)


async def fail_proven_undispatched(daemon, operation: PublicOperationRecord) -> bool:
    """Atomically fail one operation proven not to have dispatched a mutation."""
    timestamp = math.nextafter(max(now(), operation.updated_at), math.inf)
    rollback_workspace_id: str | None = None
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
            retired_participant, finished_job, rollback_workspace_id = _rollback_accepted_spawn(
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
        operation_event = daemon.operation_service.write_replacement_in_unit(
            unit,
            expected_state=current.state,
            expected_updated_at=current.updated_at,
            replacement=updated,
            idempotency_settled_at=timestamp,
        )
        if operation_event is None:
            raise RuntimeError("accepted operation changed during restart recovery")
        events.append(operation_event)
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
                lambda handle=finished_job.handle: daemon.jobs.wake_committed_direct_finish(handle)
            )
        if retired_participant is not None:
            unit.after_commit(
                lambda participant_id=retired_participant.id: daemon.registry.mark_dead(
                    participant_id
                )
            )
    if rollback_workspace_id is not None:
        service = getattr(daemon, "workspace_service", None)
        rollback = getattr(service, "rollback_created_reservation_after_recovery", None)
        if callable(rollback):
            await rollback(
                workspace_id=rollback_workspace_id,
                reservation_id=operation.operation_id,
            )
    return True


def _has_possible_dispatch(daemon, operation: PublicOperationRecord, *, connection) -> bool:
    if operation.kind == "workspace_cleanup":
        return operation.phase != "workspace_cleanup_accepted"
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
) -> tuple[Participant | None, Job | None, str | None]:
    launch = daemon.store.operations.get_launch(operation.operation_id, connection=unit.connection)
    if launch is None or launch.dispatch_marker is not None:
        return None, None, None
    participant = daemon.store.get_participant(launch.participant_id, connection=unit.connection)
    retired = _retire_reserved_participant(
        daemon, participant, timestamp=timestamp, unit=unit, events=events
    )
    finished = _finish_reserved_job(
        daemon, operation.job_handle, timestamp=timestamp, unit=unit, events=events
    )
    rollback_workspace_id: str | None = None
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
            events.append(
                workspace_usage_event(
                    daemon.store,
                    replace(
                        usage,
                        released_at=timestamp,
                        release_reason="daemon_restarted",
                    ),
                    unit.connection,
                    revision=0,
                    recorded_at=timestamp,
                )
            )
            workspace = daemon.store.workspaces.get(usage.workspace_id, connection=unit.connection)
            if (
                workspace is not None
                and workspace.creation_operation_id == operation.operation_id
                and workspace.state == "active"
                and not daemon.store.workspaces.active_usages(
                    workspace.workspace_id, connection=unit.connection
                )
            ):
                service = getattr(daemon, "workspace_service", None)
                prepare = getattr(service, "begin_creation_rollback_in_unit", None)
                if not callable(prepare):
                    raise RuntimeError("workspace rollback service is unavailable")
                prepared = prepare(
                    workspace_id=workspace.workspace_id,
                    reservation_id=operation.operation_id,
                    timestamp=timestamp,
                    unit=unit,
                )
                if prepared is None:
                    raise RuntimeError("workspace rollback intent could not be persisted")
                rollback_workspace_id = workspace.workspace_id
    unit.connection.execute(
        update(launch_reservations)
        .where(launch_reservations.c.operation_id == operation.operation_id)
        .values(phase="rolled_back", updated_at=timestamp)
    )
    return retired, finished, rollback_workspace_id


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
        participant_event(
            daemon.store,
            retired,
            unit.connection,
            revision=0,
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
    outcome = daemon.jobs.write_direct_terminal_in_unit(
        daemon.store,
        unit,
        job=job,
        state=JobState.CRASHED,
        result="the daemon restarted before launch dispatch",
        error_code="daemon_restarted",
        finished_at=timestamp,
    )
    if outcome is None:
        return None
    finished, event = outcome
    events.append(event)
    return finished


def prepare_provider_control_recovery(daemon) -> None:
    """Restore provider-only barriers before native or provider admission."""
    native_ids = {
        binding.participant_id for binding in daemon.store.runtime_bindings_for_recovery()
    }
    provider_ids = {
        participant.id
        for participant in daemon.registry.list()
        if daemon.store.terminal_bindings.get(participant.id) is not None
    }
    provider_only = sorted(provider_ids - native_ids)
    if provider_only:
        daemon.controls.fail_undelivered_followups(provider_only)


async def reconcile_public_control_operations(daemon) -> None:
    """Reconnect public operation state to durable control rows after a crash."""
    await reconcile_workspace_lifecycle(daemon)
    cursor: str | None = None
    while True:
        records, cursor = daemon.store.operations.list_page(
            cursor=cursor,
            limit=500,
            unsettled_only=True,
        )
        for operation in records:
            await _reconcile_public_control_operation(daemon, operation)
        if cursor is None:
            break


async def _reconcile_public_control_operation(daemon, operation) -> None:
    if operation.kind == "workspace_cleanup" and operation.state in {
        PublicOperationState.RUNNING.value,
        PublicOperationState.UNCERTAIN.value,
    }:
        if operation.state == PublicOperationState.RUNNING.value:
            _mark_operation_uncertain(
                daemon,
                operation.operation_id,
                "workspace_cleanup_recovery_pending",
                error_code="daemon_restarted",
                message=(
                    "the daemon restarted after workspace cleanup may have begun; "
                    "durable workspace evidence is required"
                ),
            )
        await daemon.operation_service.reconcile(operation.operation_id)
        return
    launch = (
        daemon.store.operations.get_launch(operation.operation_id)
        if operation.kind == "spawn"
        else None
    )
    if launch is not None and launch.dispatch_marker is not None:
        _restore_launch_provider_target(daemon, operation, launch)
        _mark_operation_uncertain(
            daemon, operation.operation_id, "terminal_create_recovery_pending"
        )
        return
    operation, control = _control_for_public_operation(daemon, operation)
    if control is None:
        if operation.state == PublicOperationState.ACCEPTED.value:
            if not await fail_proven_undispatched(daemon, operation):
                _mark_operation_uncertain(
                    daemon, operation.operation_id, "dispatch_recovery_pending"
                )
            return
        await _mark_crash_ambiguous_provider_operation(daemon, operation)
        return
    _reconcile_public_from_control(daemon, operation, control)


def _reconcile_public_from_control(daemon, operation, control) -> None:
    if control.delivery_phase not in {
        ControlDeliveryPhase.RESERVED,
        ControlDeliveryPhase.QUEUED,
    }:
        operation = _restore_control_dispatch_target(daemon, operation, control)
    if control.delivery_phase is ControlDeliveryPhase.QUEUED:
        operation = _ensure_running_for_recovery(daemon, operation)
        daemon.operation_service.resume(
            operation.operation_id,
            side_effect=lambda: _wait_for_recovered_control(daemon, control.operation_id),
        )
        return
    if control.delivery_phase is ControlDeliveryPhase.RESERVED:
        daemon.controls.fail_undelivered_followups([control.participant_id])
        settled = daemon.store.get_control_operation(control.operation_id)
        if settled is not None and settled.delivery_phase is ControlDeliveryPhase.SETTLED:
            if settled.delivery_result is DeliveryResult.REJECTED:
                _settle_public_from_control(
                    daemon, operation.operation_id, accepted=False, control=settled
                )
            else:
                _mark_operation_uncertain(
                    daemon, operation.operation_id, "delivery_recovery_pending"
                )
        return
    if control.delivery_phase is ControlDeliveryPhase.DISPATCHED:
        _mark_operation_uncertain(daemon, operation.operation_id, "delivery_recovery_pending")
        return
    if control.delivery_result is DeliveryResult.ACCEPTED:
        _settle_public_from_control(daemon, operation.operation_id, accepted=True, control=control)
    elif control.delivery_result is DeliveryResult.REJECTED:
        _settle_public_from_control(daemon, operation.operation_id, accepted=False, control=control)
    else:
        _mark_operation_uncertain(daemon, operation.operation_id, "delivery_recovery_pending")


def _control_for_public_operation(daemon, operation):
    control_id = operation.control_operation_id
    if control_id is None and operation.kind.startswith("controls."):
        candidate = f"{operation.operation_id}:control"
        if daemon.store.get_control_operation(candidate) is not None:
            if operation.state in {
                PublicOperationState.ACCEPTED.value,
                PublicOperationState.RUNNING.value,
            }:
                operation = daemon.operation_service.link(
                    operation.operation_id,
                    phase="control_recovered",
                    control_operation_id=candidate,
                )
            control_id = candidate
    control = None if control_id is None else daemon.store.get_control_operation(control_id)
    return operation, control


def _restore_launch_provider_target(daemon, operation, launch) -> None:
    generation = launch.launch_facts.get("provider_generation")
    if operation.dispatch_provider_id is not None or type(generation) is not int or generation < 0:
        return
    current = _ensure_running_for_recovery(daemon, operation)
    daemon.operation_service.mark_provider_dispatch_target(
        current.operation_id,
        provider_id=launch.provider_id,
        provider_generation=generation,
        phase="terminal_create_dispatch_recovered",
    )


def _restore_control_dispatch_target(daemon, operation, control):
    if operation.state == PublicOperationState.ACCEPTED.value:
        binding = daemon.store.terminal_bindings.get(control.participant_id)
        exact_terminal = binding is not None and (
            binding.provider_id,
            binding.provider_generation,
            binding.terminal_id,
            binding.terminal_incarnation,
        ) == (
            control.provider_id,
            control.provider_generation,
            control.terminal_id,
            control.terminal_incarnation,
        )
        return daemon.operation_service.mark_dispatch_intent(
            operation.operation_id,
            DispatchIntent(
                phase="control_dispatch_recovered",
                provider_id=control.provider_id,
                provider_generation=control.provider_generation,
                terminal_id=control.terminal_id if exact_terminal else None,
                terminal_incarnation=(control.terminal_incarnation if exact_terminal else None),
                occupant_evidence=(binding.occupant_evidence if exact_terminal else None),
                process_facts=(binding.process_facts if exact_terminal else None),
                backend_generation=control.backend_generation,
                native_session_id=control.native_session_id,
                native_turn_id=control.native_turn_id,
            ),
        )
    if (
        operation.dispatch_provider_id is not None
        or control.provider_id is None
        or control.provider_generation is None
    ):
        return operation
    current = _ensure_running_for_recovery(daemon, operation)
    return daemon.operation_service.mark_provider_dispatch_target(
        current.operation_id,
        provider_id=control.provider_id,
        provider_generation=control.provider_generation,
        phase="control_dispatch_recovered",
    )


def _ensure_running_for_recovery(daemon, operation):
    if operation.state == PublicOperationState.ACCEPTED.value:
        return daemon.operation_service.mark_running(
            operation.operation_id, phase="dispatch_recovered"
        )
    return operation


async def _mark_crash_ambiguous_provider_operation(daemon, operation) -> None:
    if operation.state != PublicOperationState.RUNNING.value:
        return
    dispatched = operation.dispatch_provider_id is not None
    if operation.kind == "spawn":
        launch = daemon.store.operations.get_launch(operation.operation_id)
        dispatched = launch is not None and launch.dispatch_marker is not None
        if not dispatched:
            await fail_proven_undispatched(daemon, operation)
            return
    if operation.kind == "adopt":
        participant_id = operation.target_ids[0] if len(operation.target_ids) == 1 else None
        binding = (
            daemon.store.terminal_bindings.get(participant_id)
            if participant_id is not None
            else None
        )
        if binding is not None:
            daemon.operation_service.succeed(
                operation.operation_id,
                phase="terminal_adopted_recovered",
                result={"participant_id": participant_id},
            )
        else:
            await fail_proven_undispatched(daemon, operation)
        return
    if dispatched:
        _mark_operation_uncertain(daemon, operation.operation_id, "provider_recovery_pending")
        return
    _mark_operation_uncertain(
        daemon,
        operation.operation_id,
        "mutation_recovery_pending",
        error_code="daemon_restarted",
        message=(
            "the daemon restarted after mutation execution may have begun; "
            "authoritative completion evidence is required"
        ),
    )


def _mark_operation_uncertain(
    daemon,
    operation_id: str,
    phase: str,
    *,
    error_code: str = "provider_unavailable",
    message: str = "the daemon restarted after dispatch; exact provider evidence is required",
) -> None:
    current = daemon.operation_service.get(operation_id)
    current = _ensure_running_for_recovery(daemon, current)
    if current.state == PublicOperationState.RUNNING.value:
        daemon.operation_service.mark_uncertain(
            operation_id,
            phase=phase,
            error={
                "code": error_code,
                "message": message,
            },
        )


def _settle_public_from_control(daemon, operation_id: str, *, accepted: bool, control) -> None:
    current = daemon.operation_service.get(operation_id)
    if accepted:
        current = _ensure_running_for_recovery(daemon, current)
    if current.state not in {
        PublicOperationState.ACCEPTED.value,
        PublicOperationState.RUNNING.value,
        PublicOperationState.UNCERTAIN.value,
    }:
        return
    if accepted:
        daemon.operation_service.succeed(
            operation_id,
            phase="delivery_recovered",
            result={"delivery": "accepted"},
        )
        return
    daemon.operation_service.fail(
        operation_id,
        phase="delivery_recovered",
        error={
            "code": (control.error_code or "dispatch_failed")[:MAX_ERROR_CODE_CHARS],
            "message": (control.error or "the control was rejected before restart")[
                :MAX_ERROR_MESSAGE_CHARS
            ],
        },
    )


async def _wait_for_recovered_control(daemon, control_id: str) -> OperationOutcome:
    control = await daemon.controls.wait_control_settled(control_id)
    if control is None:
        return OperationOutcome.failed(
            phase="control_missing",
            error={"code": "internal", "message": "control reservation disappeared"},
        )
    if control.delivery_result is DeliveryResult.ACCEPTED:
        return OperationOutcome.succeeded(
            phase="delivery_recovered", result={"delivery": "accepted"}
        )
    if control.delivery_result is DeliveryResult.REJECTED:
        return OperationOutcome.failed(
            phase="delivery_recovered",
            error={
                "code": (control.error_code or "dispatch_failed")[:MAX_ERROR_CODE_CHARS],
                "message": (control.error or "the control was rejected")[:MAX_ERROR_MESSAGE_CHARS],
            },
        )
    return OperationOutcome.uncertain(
        phase="delivery_recovery_pending",
        error={
            "code": (control.error_code or "delivery_unknown")[:MAX_ERROR_CODE_CHARS],
            "message": (control.error or "the delivery outcome remains unknown")[
                :MAX_ERROR_MESSAGE_CHARS
            ],
        },
    )


__all__ = [
    "fail_proven_undispatched",
    "prepare_provider_control_recovery",
    "reconcile_public_control_operations",
    "reconcile_workspace_lifecycle",
]
