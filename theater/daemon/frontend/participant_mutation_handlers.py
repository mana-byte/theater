"""Public participant mutations sharing the private actor-aware policy."""

from __future__ import annotations

from types import MappingProxyType

from theater.daemon.control_ownership import ControlTransferService
from theater.daemon.controls.routing import ControlRoute
from theater.daemon.frontend.handshake import ConnectionContext
from theater.daemon.frontend.mutation_errors import operation_error
from theater.daemon.operations import DispatchIntent, OperationOutcome, PreparedOperation
from theater.daemon.persistence.repositories.runtime_bindings import ParticipantRuntimeBinding
from theater.daemon.presence import access as presence_access
from theater.daemon.rpc.participants import (
    authorize_participant_mutation,
    persist_participant_status,
    terminate_participant,
    update_participant_metadata,
)
from theater.models import (
    PublicOperationRecord,
    PublicOperationState,
    Status,
    now,
)


def _project(daemon, participant) -> dict[str, object]:
    owner_kind = (
        participant.control_owner_kind.value
        if participant.control_owner_kind is not None
        else "participant"
        if participant.parent_id is not None
        else "local_operator"
    )
    owner: dict[str, object] = {
        "kind": owner_kind,
        "revision": participant.control_revision,
    }
    if owner_kind == "participant":
        owner["participant_id"] = participant.control_owner_id or participant.parent_id
    route = daemon.controls.terminal_route_for(participant.id)
    terminal = route.terminal
    terminal_route = None
    if terminal is not None:
        terminal_route = {
            "identity": {
                "provider_id": terminal.provider_id,
                "provider_generation": terminal.provider_generation,
                "terminal_id": terminal.terminal_id,
                "terminal_incarnation": terminal.terminal_incarnation,
                "occupant": dict(terminal.occupant_evidence),
                "process": (
                    None if terminal.process_facts is None else dict(terminal.process_facts)
                ),
            },
            "health": terminal.health if route.route_available else "offline",
        }
    return {
        "participant_id": participant.id,
        "origin": (participant.origin or participant.tier).value,
        "harness": participant.harness,
        "status": participant.status.value,
        "owner": owner,
        "parent_id": participant.parent_id,
        "cwd": participant.cwd,
        "workspace_id": participant.workspace_id,
        "name": participant.name,
        "description": participant.description,
        "addressable": route.route_available or participant.addressable,
        "presence": daemon.presence.snapshot(participant.id).state.value,
        "terminal_route": terminal_route,
        "actions": {},
    }


async def participants_update(
    daemon,
    context: ConnectionContext,
    params: dict,
    *,
    idempotency_key: str,
) -> object:
    def action(unit):
        participant = daemon.registry.resolve(str(params["participant_id"]))
        updated = update_participant_metadata(
            daemon,
            participant.id,
            caller_id="cli",
            name=params.get("name") if "name" in params else participant.name,
            description=(
                params.get("description") if "description" in params else participant.description
            ),
            unit=unit,
        )
        return _project(daemon, updated)

    return daemon.operation_service.execute_idempotent(
        client_id=context.client_id,
        idempotency_key=idempotency_key,
        method="frontend.participants.update",
        params=params,
        action=action,
    ).value


async def participants_status(
    daemon,
    context: ConnectionContext,
    params: dict,
    *,
    idempotency_key: str,
) -> object:
    replay = daemon.operation_service.replay_idempotent_write(
        client_id=context.client_id,
        idempotency_key=idempotency_key,
        method="frontend.participants.status",
        params=params,
    )
    if replay is not None:
        return replay.value
    participant_id = str(params["participant_id"])
    target = daemon.registry.resolve(participant_id)
    authorize_participant_mutation(target, "cli")
    await presence_access.require_absent(daemon, participant_id)

    def action(unit):
        updated = persist_participant_status(
            daemon,
            participant_id,
            status=Status(str(params["status"])),
            unit=unit,
        )
        return _project(daemon, updated)

    return daemon.operation_service.execute_idempotent(
        client_id=context.client_id,
        idempotency_key=idempotency_key,
        method="frontend.participants.status",
        params=params,
        action=action,
    ).value


async def participants_terminate(
    daemon,
    context: ConnectionContext,
    params: dict,
    *,
    idempotency_key: str,
) -> object:
    participant_id = str(params["participant_id"])
    captured: dict[str, object] = {}

    def prepare(operation_id, _unit):
        target = daemon.registry.resolve(participant_id)
        authorize_participant_mutation(target, "cli")
        route = daemon.controls.terminal_route_for(participant_id)
        captured["route"] = route
        captured["runtime_binding"] = daemon.store.get_runtime_binding(participant_id)
        timestamp = now()
        return PreparedOperation(
            record=PublicOperationRecord(
                operation_id=operation_id,
                kind="participants.terminate",
                actor_client_id=context.client_id,
                actor_participant_id=None,
                target_ids=(participant_id,),
                state=PublicOperationState.ACCEPTED.value,
                phase="accepted",
                created_at=timestamp,
                updated_at=timestamp,
            ),
            response={
                "operation_id": operation_id,
                "state": "accepted",
                "participant_id": participant_id,
            },
        )

    acceptance = daemon.operation_service.accept_operation(
        client_id=context.client_id,
        idempotency_key=idempotency_key,
        method="frontend.participants.terminate",
        params=params,
        prepare=prepare,
    )
    if acceptance.replayed:
        return dict(acceptance.response)
    route = captured["route"]
    assert isinstance(route, ControlRoute)
    terminal = route.terminal
    runtime_binding = captured["runtime_binding"]
    assert runtime_binding is None or isinstance(runtime_binding, ParticipantRuntimeBinding)
    native_binding = (
        runtime_binding
        if runtime_binding is not None and runtime_binding.native_session_id is not None
        else None
    )
    if terminal is not None:
        dispatch = DispatchIntent(
            phase="termination_preparing",
            provider_id=terminal.provider_id,
            provider_generation=terminal.provider_generation,
            terminal_id=terminal.terminal_id,
            terminal_incarnation=terminal.terminal_incarnation,
            occupant_evidence=terminal.occupant_evidence,
            process_facts=terminal.process_facts,
            backend_generation=(
                native_binding.backend_generation if native_binding is not None else None
            ),
            native_session_id=(
                native_binding.native_session_id if native_binding is not None else None
            ),
            composite_termination=native_binding is not None,
        )
    elif runtime_binding is not None and runtime_binding.native_session_id is not None:
        dispatch = DispatchIntent(
            phase="termination_preparing",
            backend_generation=runtime_binding.backend_generation,
            native_session_id=runtime_binding.native_session_id,
        )
    else:
        dispatch = DispatchIntent(phase="termination_preparing")

    async def side_effect() -> OperationOutcome:
        try:
            result = await terminate_participant(
                daemon,
                participant_id,
                caller_id="cli",
                operation_id=acceptance.record.operation_id,
            )
        except Exception as exc:
            error = operation_error(exc, default_code="termination_failed")
            details = error.get("details")
            if isinstance(details, dict) and details.get("possibly_executed") is True:
                return OperationOutcome.uncertain(phase="exit_unverified", error=error)
            return OperationOutcome.failed(phase="termination_refused", error=error)
        return OperationOutcome.succeeded(phase="exit_verified", result=result)

    daemon.operation_service.start(
        acceptance.record.operation_id,
        dispatch=dispatch,
        side_effect=side_effect,
    )
    return dict(acceptance.response)


async def participants_transfer_control(
    daemon,
    context: ConnectionContext,
    params: dict,
    *,
    idempotency_key: str,
) -> object:
    replay = daemon.operation_service.replay_idempotent_write(
        client_id=context.client_id,
        idempotency_key=idempotency_key,
        method="frontend.participants.transfer_control",
        params=params,
    )
    if replay is not None:
        return replay.value
    requested = params["participants"]
    assert isinstance(requested, list)
    participant_ids = [str(item["participant_id"]) for item in requested]
    service = ControlTransferService(daemon)
    async with daemon.controls.hold_participant_locks(participant_ids):
        return daemon.operation_service.execute_idempotent(
            client_id=context.client_id,
            idempotency_key=idempotency_key,
            method="frontend.participants.transfer_control",
            params=params,
            action=lambda unit: service.transfer(
                requested,
                params["new_owner"],
                unit=unit,
            ),
        ).value


PARTICIPANT_MUTATION_HANDLERS = MappingProxyType(
    {
        "frontend.participants.update": participants_update,
        "frontend.participants.status": participants_status,
        "frontend.participants.terminate": participants_terminate,
        "frontend.participants.transfer_control": participants_transfer_control,
    }
)

__all__ = ["PARTICIPANT_MUTATION_HANDLERS"]
