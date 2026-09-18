"""Public control adapters over the durable control and operation services."""

from __future__ import annotations

import json
from types import MappingProxyType

from theater.daemon.frontend.handshake import ConnectionContext
from theater.daemon.frontend.mutation_errors import operation_error as _error
from theater.daemon.operations import DispatchIntent, OperationOutcome, PreparedOperation
from theater.daemon.rpc.params import _prompt_with_response_format
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    DeliveryResult,
    RuntimeCapability,
)
from theater.models import PublicOperationRecord, PublicOperationState, now


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
        "code": (operation.error_code or "delivery_unknown")[:512],
        "message": (operation.error or "the control delivery outcome is unknown")[:8192],
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
    capability = {
        "frontend.controls.send": RuntimeCapability.SEND,
        "frontend.controls.steer": RuntimeCapability.STEER,
        "frontend.controls.queue_followup": RuntimeCapability.QUEUE_FOLLOWUP,
        "frontend.controls.interrupt": RuntimeCapability.INTERRUPT,
        "frontend.controls.settings.update": RuntimeCapability.SETTINGS_UPDATE,
    }[method]
    kind = {
        "frontend.controls.send": ControlKind.SEND,
        "frontend.controls.steer": ControlKind.STEER,
        "frontend.controls.queue_followup": ControlKind.QUEUE_FOLLOWUP,
        "frontend.controls.interrupt": ControlKind.INTERRUPT,
        "frontend.controls.settings.update": ControlKind.SETTINGS_UPDATE,
    }[method]
    response_format = params.get("response_format")
    serialized_format = (
        json.dumps(response_format, sort_keys=True, separators=(",", ":"))
        if response_format is not None
        else None
    )
    prompt = (
        _prompt_with_response_format(str(params["prompt"]), serialized_format)
        if method in {"frontend.controls.send", "frontend.controls.queue_followup"}
        else str(params["prompt"])
        if method == "frontend.controls.steer"
        else None
    )
    captured: dict[str, object] = {}

    def prepare(operation_id, unit):
        daemon.registry.get(participant_id)
        route = daemon.controls.route_for(participant_id, capability)
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
    route = captured["route"]
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
                from theater.daemon.rails import check_model_allowed, check_reasoning_allowed

                target = daemon.registry.get(participant_id)
                check_model_allowed(
                    target.harness,
                    params.get("model"),
                    daemon.config.models_for(target.harness),
                )
                check_reasoning_allowed(
                    target.harness,
                    params.get("reasoning_effort"),
                    daemon.config.reasoning_for(target.harness),
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
        dispatch=_dispatch_for(route),
        side_effect=side_effect,
    )
    return dict(acceptance.response)


async def controls_get(daemon, _context: ConnectionContext, params: dict) -> dict:
    participant_id = str(params["participant_id"])
    participant = daemon.registry.get(participant_id)
    presence = daemon.presence.snapshot(participant_id).state.value
    actions: dict[str, dict[str, object]] = {}
    for name, capability in (
        ("send", RuntimeCapability.SEND),
        ("steer", RuntimeCapability.STEER),
        ("queue_followup", RuntimeCapability.QUEUE_FOLLOWUP),
        ("interrupt", RuntimeCapability.INTERRUPT),
        ("settings_update", RuntimeCapability.SETTINGS_UPDATE),
    ):
        route = daemon.controls.route_for(participant_id, capability)
        supported = route.transport is not None
        available = route.route_available
        if route.is_native:
            available = daemon.runtime_manager.get(participant_id) is not None
        admissible = supported and available and presence == "absent"
        entry: dict[str, object] = {
            "supported": supported,
            "route_available": available,
            "admissible": admissible,
        }
        if not supported:
            entry["reason"] = "unsupported"
        elif not available:
            entry["reason"] = "route_unavailable"
        elif not admissible:
            entry["reason"] = "human_presence"
        actions[name] = entry
    revision = participant.control_revision
    binding = daemon.store.terminal_bindings.get(participant_id)
    if binding is not None:
        revision = max(revision, binding.report_revision)
    return {"actions": actions, "revision": revision}


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
