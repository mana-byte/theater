"""Public control adapters over the durable control and operation services."""

from __future__ import annotations

from types import MappingProxyType

from theater.daemon.controls.report import (
    ControlActiveTurn,
    build_control_report,
    control_report_to_wire,
)
from theater.daemon.controls.settings_policy import validate_settings
from theater.daemon.frontend.handshake import ConnectionContext
from theater.daemon.operations import DispatchIntent, OperationOutcome, PreparedOperation
from theater.daemon.operations.error_values import operation_error as _error
from theater.daemon.presence import access as presence_access
from theater.daemon.response_contracts import resolve_response_format
from theater.daemon.skill_cards import compose_prompt
from theater.daemon.transcript_projection import observed_transcript_identity
from theater.frontend.capabilities import MAX_ERROR_CODE_CHARS, MAX_ERROR_MESSAGE_CHARS
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    DeliveryResult,
    RuntimeCapability,
    RuntimeWiring,
)
from theater.models import PublicOperationRecord, PublicOperationState, Status, now

_CONTROL_METHODS = MappingProxyType(
    {
        "frontend.controls.send": (RuntimeCapability.SEND, ControlKind.SEND),
        "frontend.controls.steer": (RuntimeCapability.STEER, ControlKind.STEER),
        "frontend.controls.queue_followup": (
            RuntimeCapability.QUEUE_FOLLOWUP,
            ControlKind.QUEUE_FOLLOWUP,
        ),
        "frontend.controls.interrupt": (RuntimeCapability.INTERRUPT, ControlKind.INTERRUPT),
        "frontend.controls.settings.update": (
            RuntimeCapability.SETTINGS_UPDATE,
            ControlKind.SETTINGS_UPDATE,
        ),
    }
)


def _prepared(
    operation_id: str,
    context: ConnectionContext,
    method: str,
    participant_id: str,
    *,
    control_operation_id: str | None,
    job_handle: str | None,
    events=(),
) -> PreparedOperation:
    timestamp = now()
    response: dict[str, object] = {
        "operation_id": operation_id,
        "state": PublicOperationState.ACCEPTED.value,
        "participant_id": participant_id,
    }
    if job_handle is not None:
        response["job_handle"] = job_handle
    return PreparedOperation(
        record=PublicOperationRecord(
            operation_id=operation_id,
            kind=method.removeprefix("frontend."),
            actor_client_id=context.client_id,
            actor_participant_id=None,
            target_ids=(participant_id,),
            state=PublicOperationState.ACCEPTED.value,
            phase="control_reserved",
            created_at=timestamp,
            updated_at=timestamp,
            control_operation_id=control_operation_id,
            job_handle=job_handle,
        ),
        response=response,
        events=tuple(events),
    )


def _dispatch_for(route) -> DispatchIntent:
    terminal = route.terminal
    if terminal is None:
        return DispatchIntent(phase="control_preparing")
    return DispatchIntent(
        phase="control_preparing",
        provider_id=terminal.provider_id,
        provider_generation=terminal.provider_generation,
        terminal_id=terminal.terminal_id,
        terminal_incarnation=terminal.terminal_incarnation,
        occupant_evidence=terminal.occupant_evidence,
        process_facts=terminal.process_facts,
    )


def _stored_outcome(operation) -> OperationOutcome:
    error = {
        "code": (operation.error_code or "delivery_unknown")[:MAX_ERROR_CODE_CHARS],
        "message": (operation.error or "the control delivery outcome is unknown")[
            :MAX_ERROR_MESSAGE_CHARS
        ],
    }
    if operation.delivery_phase is not ControlDeliveryPhase.SETTLED:
        return OperationOutcome.uncertain(phase="delivery_unknown", error=error)
    if operation.delivery_result is DeliveryResult.ACCEPTED:
        return OperationOutcome.succeeded(
            phase="delivery_acknowledged",
            result={"delivery": "accepted"},
        )
    if operation.delivery_result is DeliveryResult.REJECTED:
        return OperationOutcome.failed(phase="delivery_rejected", error=error)
    return OperationOutcome.uncertain(phase="delivery_unknown", error=error)


async def _await_queued(daemon, control_operation_id: str) -> OperationOutcome:
    operation = await daemon.controls.wait_control_settled(control_operation_id)
    if operation is None:
        return OperationOutcome.failed(
            phase="control_missing",
            error={"code": "internal", "message": "control reservation disappeared"},
        )
    return _stored_outcome(operation)


def _submit(
    daemon,
    context: ConnectionContext,
    method: str,
    params: dict,
    idempotency_key: str,
):
    participant_id = str(params["participant_id"])
    capability, kind = _CONTROL_METHODS[method]
    serialized_format = resolve_response_format(
        daemon.store, params.get("response_format"), creator_id=None
    )
    if method in {"frontend.controls.send", "frontend.controls.queue_followup"}:
        prompt: str | None = compose_prompt(
            str(params["prompt"]), response_format=serialized_format
        )
    elif method == "frontend.controls.steer":
        prompt = str(params["prompt"])
    else:
        prompt = None
    captured: dict[str, object] = {}

    def prepare(operation_id, unit):
        daemon.registry.get(participant_id)
        route = daemon.controls.route_for(participant_id, capability, connection=unit.connection)
        captured["route"] = route
        if kind is ControlKind.SETTINGS_UPDATE and route.transport is None:
            return _prepared(
                operation_id,
                context,
                method,
                participant_id,
                control_operation_id=None,
                job_handle=None,
            )
        settings = {
            key: str(value)
            for key in ("model", "reasoning_effort")
            if (value := params.get(key)) is not None
        }
        reservation = daemon.controls.reserve_public_control(
            unit,
            operation_id=operation_id,
            participant_id=participant_id,
            kind=kind,
            route=route,
            caller_id="cli",
            actor_client_id=context.client_id,
            prompt=prompt,
            response_format=serialized_format,
            expected_turn_id=params.get("expected_turn_id"),
            settings=settings,
        )
        return _prepared(
            operation_id,
            context,
            method,
            participant_id,
            control_operation_id=reservation.control_operation_id,
            job_handle=reservation.job_handle,
            events=reservation.events,
        )

    acceptance = daemon.operation_service.accept_operation(
        client_id=context.client_id,
        idempotency_key=idempotency_key,
        method=method,
        params=params,
        prepare=prepare,
    )
    if acceptance.replayed:
        return dict(acceptance.response)
    control_operation_id = f"{acceptance.record.operation_id}:control"
    pre_reserved = acceptance.record.control_operation_id is not None

    async def side_effect() -> OperationOutcome:
        try:
            if method == "frontend.controls.send":
                await daemon.controls.send(
                    participant_id,
                    caller_id="cli",
                    prompt=prompt,
                    response_format=serialized_format,
                    operation_id=control_operation_id,
                    callback_operation_id=acceptance.record.operation_id,
                    actor_client_id=context.client_id,
                    pre_reserved=pre_reserved,
                )
            elif method == "frontend.controls.steer":
                await daemon.controls.steer(
                    participant_id,
                    caller_id="cli",
                    prompt=prompt,
                    expected_turn_id=params.get("expected_turn_id"),
                    operation_id=control_operation_id,
                    callback_operation_id=acceptance.record.operation_id,
                    pre_reserved=pre_reserved,
                )
            elif method == "frontend.controls.queue_followup":
                await daemon.controls.queue_followup(
                    participant_id,
                    caller_id="cli",
                    prompt=prompt,
                    response_format=serialized_format,
                    operation_id=control_operation_id,
                    callback_operation_id=acceptance.record.operation_id,
                    actor_client_id=context.client_id,
                    pre_reserved=pre_reserved,
                )
            elif method == "frontend.controls.interrupt":
                outcome = await daemon.controls.interrupt(
                    participant_id,
                    caller_id="cli",
                    operation_id=control_operation_id,
                    callback_operation_id=acceptance.record.operation_id,
                    pre_reserved=pre_reserved,
                )
                if outcome.reason == "already_idle":
                    return OperationOutcome.succeeded(
                        phase="already_idle", result={"interrupted": False}
                    )
                operation = daemon.store.get_control_operation(control_operation_id)
                assert operation is not None
                return _stored_outcome(operation)
            else:
                target = daemon.registry.get(participant_id)
                validate_settings(
                    daemon.config,
                    target.harness,
                    params.get("model"),
                    params.get("reasoning_effort"),
                )
                await daemon.controls.update_settings(
                    participant_id,
                    caller_id="cli",
                    model=params.get("model"),
                    reasoning_effort=params.get("reasoning_effort"),
                    operation_id=control_operation_id,
                    pre_reserved=pre_reserved,
                )
                operation = daemon.store.get_control_operation(control_operation_id)
                if operation is None:
                    return OperationOutcome.failed(
                        phase="control_missing",
                        error={
                            "code": "internal",
                            "message": "settings control reservation was not persisted",
                        },
                    )
                return _stored_outcome(operation)
        except Exception as exc:
            operation = daemon.store.get_control_operation(control_operation_id)
            if operation is not None and (
                operation.delivery_phase is ControlDeliveryPhase.DISPATCHED
                or operation.delivery_result is DeliveryResult.UNKNOWN
            ):
                return OperationOutcome.uncertain(phase="delivery_unknown", error=_error(exc))
            daemon.controls.reject_public_reservation(control_operation_id, exc)
            return OperationOutcome.failed(phase="control_refused", error=_error(exc))
        if method == "frontend.controls.queue_followup":
            return await _await_queued(daemon, control_operation_id)
        operation = daemon.store.get_control_operation(control_operation_id)
        if operation is None:
            return OperationOutcome.failed(
                phase="control_missing",
                error={"code": "internal", "message": "control reservation disappeared"},
            )
        return _stored_outcome(operation)

    daemon.operation_service.start(
        acceptance.record.operation_id,
        dispatch=_dispatch_for(captured["route"]),
        side_effect=side_effect,
    )
    return dict(acceptance.response)


async def controls_get(daemon, _context: ConnectionContext, params: dict) -> dict:
    participant_id = str(params["participant_id"])
    participant = daemon.registry.get(participant_id)
    presence_snapshot = presence_access.presence_snapshot(daemon, participant_id)
    presence = presence_snapshot.state.value
    actions: dict[str, dict[str, object]] = {}
    for name, capability in (
        ("send", RuntimeCapability.SEND),
        ("steer", RuntimeCapability.STEER),
        ("queue_followup", RuntimeCapability.QUEUE_FOLLOWUP),
        ("interrupt", RuntimeCapability.INTERRUPT),
        ("settings_update", RuntimeCapability.SETTINGS_UPDATE),
    ):
        route = daemon.controls.route_for(participant_id, capability)
        available = route.route_available
        actions[name] = daemon.controls.project_action(
            participant_id,
            capability,
            route=route,
            route_available=available,
            alive=participant.status is not Status.DEAD,
            presence=presence,
            presence_detail=presence_snapshot.reason,
        )
    revision = participant.control_revision
    binding = daemon.store.terminal_bindings.get(participant_id)
    if binding is not None:
        revision = max(revision, binding.report_revision)
    details = _control_details(daemon, participant_id, presence_snapshot.to_dict())
    return {
        "actions": actions,
        "revision": revision,
        "transcript_identity": observed_transcript_identity(participant, daemon.observer),
        **details,
    }


def _control_details(
    daemon,
    participant_id: str,
    presence: dict[str, object],
) -> dict[str, object]:
    """Project rc9's read-only control report as additive public fields."""
    queued = [job.handle for job in daemon.controls.queued_jobs(participant_id)]
    binding = daemon.store.get_runtime_binding(participant_id)
    cached = (
        None
        if binding is None
        else daemon.runtime_manager.cached_native_facts(
            participant_id,
            backend_generation=binding.backend_generation,
            native_session_id=binding.native_session_id,
        )
    )
    active_turn = None
    if (
        cached is not None
        and isinstance(cached.native_turn_id, str)
        and isinstance(cached.native_session_id, str)
    ):
        job = daemon.controls.active_job_for_native_turn(
            participant_id,
            backend_generation=binding.backend_generation,
            native_session_id=cached.native_session_id,
            native_turn_id=cached.native_turn_id,
        )
        active_turn = ControlActiveTurn(
            cached.native_turn_id,
            job.handle if job is not None else None,
            cached.pending_interaction,
        )
    terminal_route = daemon.controls.terminal_route_for(participant_id) if binding is None else None
    report = build_control_report(
        participant_id,
        native=cached,
        binding=binding,
        provider_route=terminal_route,
        active_turn=active_turn,
        queued=queued,
        human_presence=presence,
        native_wiring=binding.wiring if binding is not None else RuntimeWiring.NATIVE,
    )
    return control_report_to_wire(report)


async def controls_send(daemon, context, params, *, idempotency_key):
    return _submit(daemon, context, "frontend.controls.send", params, idempotency_key)


async def controls_steer(daemon, context, params, *, idempotency_key):
    return _submit(daemon, context, "frontend.controls.steer", params, idempotency_key)


async def controls_queue_followup(daemon, context, params, *, idempotency_key):
    return _submit(daemon, context, "frontend.controls.queue_followup", params, idempotency_key)


async def controls_interrupt(daemon, context, params, *, idempotency_key):
    return _submit(daemon, context, "frontend.controls.interrupt", params, idempotency_key)


async def controls_settings_update(daemon, context, params, *, idempotency_key):
    return _submit(daemon, context, "frontend.controls.settings.update", params, idempotency_key)


CONTROL_HANDLERS = MappingProxyType(
    {
        "frontend.controls.get": controls_get,
        "frontend.controls.send": controls_send,
        "frontend.controls.steer": controls_steer,
        "frontend.controls.queue_followup": controls_queue_followup,
        "frontend.controls.interrupt": controls_interrupt,
        "frontend.controls.settings.update": controls_settings_update,
    }
)

__all__ = ["CONTROL_HANDLERS"]
