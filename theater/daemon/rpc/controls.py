"""Control RPC handlers: thin adapters over ``ControlService``, which owns all policy.

Params are validated before any state is touched, so a malformed call never reserves an
operation or mints a job; responses are additive (wire protocol stays version 1).
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from theater.daemon.presence import access as presence_access
from theater.daemon.rails import check_model_allowed, check_reasoning_allowed
from theater.daemon.rpc.params import (
    _optional_string_param,
    _prompt_with_response_format,
    _serialized_response_format,
    _string_param,
)
from theater.daemon.rpc.router import method
from theater.daemon.transcript_projection import observed_transcript_identity
from theater.harness import HARNESSES, normalize
from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    ConnectionHealth,
    ControlKind,
    DeliveryResult,
    RuntimeCapability,
    RuntimeHost,
    RuntimeWiring,
)

#: The one-word reason a pane without native wiring cannot offer a native
#: control. The detail string says the same thing the service's refusal does.
_WIRING_REASON = str(CapabilityUnavailableReason.WIRING_MODE)
_STEER_LEGACY_DETAIL = (
    "steering requires native runtime wiring; this participant's harness has "
    "no runtime — send when it is idle or queue a followup"
)
_SETTINGS_LEGACY_DETAIL = (
    "settings updates require native runtime wiring; this participant's model is fixed at launch"
)
_NO_INTERRUPT_CONTROL_DETAIL = (
    "the harness declares no interrupt control; its plugin owner must add controls.interrupt"
)
#: Stable capability report order.
_CAPABILITY_ORDER = (
    RuntimeCapability.SEND,
    RuntimeCapability.STEER,
    RuntimeCapability.QUEUE_FOLLOWUP,
    RuntimeCapability.SETTINGS_UPDATE,
    RuntimeCapability.INTERRUPT,
)


def _capability_entry(*, available: bool, reason: str | None, detail: str | None) -> dict:
    entry: dict = {"available": available}
    if reason is not None:
        entry["reason"] = reason
    if detail is not None:
        entry["detail"] = detail
    return entry


def _native_capabilities(capabilities) -> dict:
    """One effective answer per capability, with the runtime's own reason.

    ``queue_followup`` tracks SEND exactly: the queue is Theater-owned FIFO, so a runtime
    marking its native queue unavailable (Codex: ``theater_policy``) still gets Theater's.
    """
    report: dict = {}
    send_entry: dict = {}
    for capability in _CAPABILITY_ORDER:
        if capability is RuntimeCapability.SEND:
            if capabilities.supports(capability):
                send_entry = _capability_entry(available=True, reason=None, detail=None)
            else:
                reason = capabilities.reason_for(capability)
                send_entry = _capability_entry(
                    available=False,
                    reason=str(reason) if reason is not None else None,
                    detail=None,
                )
            report[capability.value] = send_entry
        elif capability is RuntimeCapability.QUEUE_FOLLOWUP:
            report[capability.value] = _effective_queue_capability(send_entry)
        elif capabilities.supports(capability):
            report[capability.value] = _capability_entry(available=True, reason=None, detail=None)
        else:
            reason = capabilities.reason_for(capability)
            report[capability.value] = _capability_entry(
                available=False,
                reason=str(reason) if reason is not None else None,
                detail=None,
            )
    return report


def _effective_queue_capability(send_entry: dict) -> dict:
    """The public followup capability: the queue tracks the dispatchable SEND."""
    if send_entry.get("available"):
        return _capability_entry(available=True, reason=None, detail=None)
    reason = send_entry.get("reason") or _WIRING_REASON
    return _capability_entry(
        available=False,
        reason=reason,
        detail=(
            "the followup queue is Theater-owned and dispatches through the "
            f"native send capability, which is unavailable ({reason})"
        ),
    )


def _legacy_capabilities(target) -> dict:
    """Effective capabilities for pane-wired (no runtime) participants.

    Send and the Theater-owned queue work on panes; steer and settings need native wiring;
    interrupt only when the harness declares a pane path.
    """
    harness = HARNESSES.get(normalize(target.harness))
    controls = None if harness is None else getattr(harness, "controls", None)
    interrupt_plan = None if controls is None else getattr(controls, "interrupt", None)
    report: dict = {}
    for capability in _CAPABILITY_ORDER:
        if capability in (RuntimeCapability.SEND, RuntimeCapability.QUEUE_FOLLOWUP):
            report[capability.value] = _capability_entry(available=True, reason=None, detail=None)
        elif capability is RuntimeCapability.STEER:
            report[capability.value] = _capability_entry(
                available=False, reason=_WIRING_REASON, detail=_STEER_LEGACY_DETAIL
            )
        elif capability is RuntimeCapability.SETTINGS_UPDATE:
            report[capability.value] = _capability_entry(
                available=False, reason=_WIRING_REASON, detail=_SETTINGS_LEGACY_DETAIL
            )
        else:
            report[capability.value] = (
                _capability_entry(available=True, reason=None, detail=None)
                if interrupt_plan is not None
                else _capability_entry(
                    available=False, reason=_WIRING_REASON, detail=_NO_INTERRUPT_CONTROL_DETAIL
                )
            )
    return report


def _effective_capabilities(daemon, target, snapshot=None) -> dict:
    legacy = _legacy_capabilities(target)
    native = _native_capabilities(snapshot.capabilities) if snapshot is not None else {}
    runtime_host = _runtime_host(daemon, target)
    report: dict = {}
    for capability in _CAPABILITY_ORDER:
        route = daemon.controls.route_for(target.id, capability)
        if route.is_provider:
            entry = _capability_entry(
                available=route.route_available,
                reason=None if route.route_available else "provider_unavailable",
                detail=None,
            )
        elif route.is_legacy:
            entry = legacy[capability.value]
        elif route.is_native and snapshot is not None:
            entry = native[capability.value]
        elif route.is_native:
            entry = _capability_entry(
                available=False,
                reason=_WIRING_REASON,
                detail="the selected native runtime is not connected",
            )
        else:
            entry = _capability_entry(
                available=False,
                reason=str(route.unavailable_reason or CapabilityUnavailableReason.WIRING_MODE),
                detail="the selected runtime does not support this capability",
            )
        entry["transport"] = (
            "provider"
            if route.is_provider
            else str(route.transport)
            if route.transport is not None
            else None
        )
        if route.is_native:
            entry["runtime_host"] = str(runtime_host or RuntimeHost.DETACHED_BACKEND)
        if capability is RuntimeCapability.SETTINGS_UPDATE:
            supported = (
                snapshot.settings.supported_fields
                if route.is_native and snapshot is not None
                else ()
            )
            entry["supported_fields"] = sorted(str(field) for field in supported)
        report[capability.value] = entry
    return report


def _runtime_host(daemon, target) -> RuntimeHost | None:
    binding = daemon.store.get_runtime_binding(target.id)
    if binding is not None and binding.launch_policy:
        try:
            policy = json.loads(binding.launch_policy)
        except ValueError:
            policy = {}
        value = policy.get("runtime_host") if isinstance(policy, Mapping) else None
        if isinstance(value, str):
            try:
                return RuntimeHost(value)
            except ValueError:
                pass
    harness = HARNESSES.get(normalize(target.harness))
    runtime = None if harness is None else getattr(harness, "runtime", None)
    host = None if runtime is None else getattr(runtime, "host", None)
    return host if isinstance(host, RuntimeHost) else None


def _interaction(interaction) -> dict | None:
    """Serialize one pending native human interaction, or ``None``."""
    if interaction is None:
        return None
    entry: dict = {"kind": str(interaction.kind)}
    if interaction.native_turn_id is not None:
        entry["native_turn_id"] = interaction.native_turn_id
    if interaction.details:
        entry["details"] = interaction.details
    return entry


def _steer_receipt(daemon, job) -> dict:
    """The flat additive delivery facts for the steer that just finished.

    Settled under the service lock and read synchronously, so no steer can overtake it. If
    absent, report unknown delivery with a consistency reason — never optimistic success.
    """
    steer_operations = [
        operation
        for operation in daemon.store.control_operations_for_job(job.handle)
        if operation.kind is ControlKind.STEER
    ]
    latest = (
        max(steer_operations, key=lambda item: (item.updated_at, item.created_at))
        if steer_operations
        else None
    )
    if latest is None:
        return {
            "delivery": "unknown",
            "phase": None,
            "operation_id": None,
            "reason": "steer_operation_missing",
            "detail": (
                f"the persisted STEER operation for job {job.handle!r} could not "
                "be read back after the service returned; the amendment's "
                "delivery is unknown — do not retry blindly"
            ),
        }
    receipt: dict = {
        "delivery": "accepted" if latest.delivery_result is DeliveryResult.ACCEPTED else "unknown",
        "phase": str(latest.delivery_phase),
        "operation_id": latest.operation_id,
    }
    if latest.error_code is not None:
        receipt["reason"] = latest.error_code
    if latest.error is not None:
        receipt["detail"] = latest.error
    return receipt


@method("participant.steer")
async def _steer(daemon, params: dict) -> dict:
    """Amend exactly the current Theater job's active native turn.

    Returns the still-running job plus flat delivery fields read back from the persisted
    STEER, so callers can tell an accepted amendment from an unknown delivery.
    """
    method_name = "participant.steer"
    target = daemon.registry.resolve(_string_param(params, "target", method_name=method_name))
    prompt = _string_param(params, "prompt", method_name=method_name)
    caller_id = _string_param(params, "caller_id", method_name=method_name)
    job_handle = _optional_string_param(params, "job_handle", method_name=method_name)
    job = await daemon.controls.steer(
        target.id,
        caller_id=caller_id,
        prompt=prompt,
        job_handle=job_handle,
    )
    result = job.to_dict()
    result.update(_steer_receipt(daemon, job))
    daemon.store.bus_append(
        "agent.steer",
        from_id=caller_id,
        to_id=target.id,
        payload={
            "handle": job.handle,
            "prompt": prompt[:200],
            "delivery": result["delivery"],
        },
    )
    return result


@method("participant.queue_followup")
async def _queue_followup(daemon, params: dict) -> dict:
    """Create an awaitable send job now; it dispatches on the next idle.

    Response-format guidance is injected once, at queue time, so neither dispatch path
    re-injects it.
    """
    method_name = "participant.queue_followup"
    target = daemon.registry.resolve(_string_param(params, "target", method_name=method_name))
    prompt = _string_param(params, "prompt", method_name=method_name)
    caller_id = _string_param(params, "caller_id", method_name=method_name)
    response_format = _serialized_response_format(params)
    job = await daemon.controls.queue_followup(
        target.id,
        caller_id=caller_id,
        prompt=_prompt_with_response_format(prompt, response_format),
        response_format=response_format,
    )
    daemon.store.bus_append(
        "agent.queue_followup",
        from_id=caller_id,
        to_id=target.id,
        payload={"handle": job.handle, "prompt": prompt[:200]},
    )
    return job.to_dict()


@method("participant.settings.update")
async def _settings_update(daemon, params: dict) -> dict:
    """Idle-only model/reasoning change; approval and sandbox stay untouched.

    Allowlists are enforced here because the daemon holds its start-up config, same as spawn.
    """
    method_name = "participant.settings.update"
    target = daemon.registry.resolve(_string_param(params, "target", method_name=method_name))
    caller_id = _string_param(params, "caller_id", method_name=method_name)
    model = _optional_string_param(params, "model", method_name=method_name)
    reasoning_effort = _optional_string_param(params, "reasoning_effort", method_name=method_name)
    check_model_allowed(target.harness, model, daemon.config.models_for(target.harness))
    check_reasoning_allowed(
        target.harness, reasoning_effort, daemon.config.reasoning_for(target.harness)
    )
    outcome = await daemon.controls.update_settings(
        target.id,
        caller_id=caller_id,
        model=model,
        reasoning_effort=reasoning_effort,
    )
    result: dict = {
        "id": target.id,
        "applied": outcome.applied,
        "model": outcome.model,
        "reasoning_effort": outcome.reasoning_effort,
    }
    if outcome.error_code is not None:
        result["error_code"] = outcome.error_code
    if outcome.error is not None:
        result["error"] = outcome.error
    return result


@method("participant.controls")
async def _controls(daemon, params: dict) -> dict:
    """Effective capabilities, health, settings, active turn, queued handles.

    Read-only: a live runtime is only read, never created — a controls
    inspection must never launch a backend or open a control connection.
    """
    method_name = "participant.controls"
    target = daemon.registry.resolve(_string_param(params, "target", method_name=method_name))
    pid = target.id
    presence = presence_access.presence_snapshot(daemon, pid).to_dict()
    transcript = observed_transcript_identity(target, getattr(daemon, "observer", None))
    queued = [job.handle for job in daemon.controls.queued_jobs(pid)]
    runtime = daemon.runtime_manager.get(pid)
    if runtime is not None:
        snapshot = await runtime.snapshot()
        daemon.runtime_manager.record_snapshot(pid, runtime, snapshot)
        active_turn: dict | None = None
        if snapshot.native_turn_id is not None:
            job = daemon.controls.active_job_for_native_turn(
                pid,
                backend_generation=snapshot.backend_generation,
                native_session_id=snapshot.native_session_id,
                native_turn_id=snapshot.native_turn_id,
            )
            active_turn = {
                "native_turn_id": snapshot.native_turn_id,
                "job_handle": job.handle if job is not None else None,
            }
            interaction = _interaction(snapshot.pending_interaction)
            if interaction is not None:
                active_turn["pending_interaction"] = interaction
        return {
            "id": pid,
            "wiring": str(RuntimeWiring.NATIVE),
            "backend_generation": snapshot.backend_generation,
            "native_session_id": snapshot.native_session_id,
            "health": {
                "connection": str(snapshot.health),
                "diagnostics": list(snapshot.health_diagnostics),
            },
            "settings": {
                "model": snapshot.settings.model,
                "reasoning_effort": snapshot.settings.reasoning_effort,
            },
            "capabilities": _effective_capabilities(daemon, target, snapshot),
            "transcript_identity": transcript,
            "active_turn": active_turn,
            "queued": queued,
            "human_presence": presence,
        }
    binding = daemon.store.get_runtime_binding(pid)
    if binding is not None:
        # Native-wired, but its runtime is not connected right now (for
        # example between a daemon restart and recovery's reconnect). Report
        # the persisted wiring honestly instead of pretending it is legacy.
        return {
            "id": pid,
            "wiring": str(binding.wiring),
            "backend_generation": binding.backend_generation,
            "native_session_id": binding.native_session_id,
            "health": {
                "connection": str(ConnectionHealth.DISCONNECTED),
                "diagnostics": ["no runtime connection: the daemon reconnects during recovery"],
            },
            "settings": None,
            "capabilities": _effective_capabilities(daemon, target),
            "transcript_identity": transcript,
            "active_turn": None,
            "queued": queued,
            "human_presence": presence,
        }
    terminal_route = daemon.controls.terminal_route_for(pid)
    if terminal_route.is_provider and terminal_route.terminal is not None:
        terminal = terminal_route.terminal
        return {
            "id": pid,
            "wiring": "provider",
            "backend_generation": None,
            "native_session_id": None,
            "health": {
                "connection": terminal_route.provider_health,
                "diagnostics": [],
            },
            "settings": None,
            "capabilities": _effective_capabilities(daemon, target),
            "transcript_identity": transcript,
            "active_turn": None,
            "queued": queued,
            "human_presence": presence,
            "provider_id": terminal.provider_id,
            "provider_generation": terminal.provider_generation,
            "terminal_id": terminal.terminal_id,
            "terminal_incarnation": terminal.terminal_incarnation,
        }
    return {
        "id": pid,
        "wiring": str(RuntimeWiring.LEGACY),
        "backend_generation": None,
        "native_session_id": None,
        "health": None,
        "settings": None,
        "capabilities": _effective_capabilities(daemon, target),
        "transcript_identity": transcript,
        "active_turn": None,
        "queued": queued,
        "human_presence": presence,
    }
