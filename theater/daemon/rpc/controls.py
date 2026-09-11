"""Control RPC handlers: steer, queue, settings, and controls inspection.

Thin boundary adapters over the already-integrated
:class:`~theater.daemon.controls.service.ControlService`: these handlers
validate parameters at the daemon boundary and serialize results. Every
policy decision — authorization, idle checks, capability gating, queue
semantics, delivery recovery — belongs to the service and its injected gates;
none of it is duplicated here.

Malformed or missing parameters fail here, before any state is touched, so a
malformed call can never reserve an operation or mint a job. Responses are
additive: the wire protocol stays version 1, and new methods plus new optional
response fields are the only additions.
"""

from __future__ import annotations

from theater.daemon.rails import check_model_allowed, check_reasoning_allowed
from theater.daemon.rpc.params import (
    _optional_string_param,
    _serialized_response_format,
    _string_param,
)
from theater.daemon.rpc.router import method
from theater.harness import HARNESSES, normalize
from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    ConnectionHealth,
    RuntimeCapability,
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
    """One effective answer per capability, with the runtime's own reason."""
    report: dict = {}
    for capability in _CAPABILITY_ORDER:
        if capabilities.supports(capability):
            report[capability.value] = _capability_entry(available=True, reason=None, detail=None)
            continue
        reason = capabilities.reason_for(capability)
        report[capability.value] = _capability_entry(
            available=False,
            reason=str(reason) if reason is not None else None,
            detail=None,
        )
    return report


def _legacy_capabilities(target) -> dict:
    """Effective capabilities for pane-wired (no runtime) participants.

    Ordinary send keeps its current permissions, and the followup queue is
    Theater-owned, so both work on the legacy transport. Steer and settings
    genuinely require native wiring. Interrupt exists as the pane path the
    existing RPC uses — offered exactly when the harness declares one.
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


@method("participant.steer")
async def _steer(daemon, params: dict) -> dict:
    """Amend exactly the current Theater job's active native turn."""
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
    return job.to_dict()


@method("participant.queue_followup")
async def _queue_followup(daemon, params: dict) -> dict:
    """Create an awaitable send job now; it dispatches on the next idle."""
    method_name = "participant.queue_followup"
    target = daemon.registry.resolve(_string_param(params, "target", method_name=method_name))
    prompt = _string_param(params, "prompt", method_name=method_name)
    caller_id = _string_param(params, "caller_id", method_name=method_name)
    response_format = _serialized_response_format(params)
    job = await daemon.controls.queue_followup(
        target.id,
        caller_id=caller_id,
        prompt=prompt,
        response_format=response_format,
    )
    return job.to_dict()


@method("participant.settings.update")
async def _settings_update(daemon, params: dict) -> dict:
    """Idle-only model/reasoning change; approval and sandbox stay untouched.

    The service enforces idle checks and native capability gating. The
    model/reasoning allowlists are the same config policy a spawn goes
    through, enforced here where the daemon holds its start-up config.
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
    queued = [job.handle for job in daemon.controls.queued_jobs(pid)]
    runtime = daemon.runtime_manager.get(pid)
    if runtime is not None:
        snapshot = await runtime.snapshot()
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
            "capabilities": _native_capabilities(snapshot.capabilities),
            "active_turn": active_turn,
            "queued": queued,
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
            "capabilities": {
                capability.value: _capability_entry(
                    available=False,
                    reason=_WIRING_REASON,
                    detail=(
                        "the participant's native runtime is not connected; "
                        "the daemon reconnects it during recovery"
                    ),
                )
                for capability in _CAPABILITY_ORDER
            },
            "active_turn": None,
            "queued": queued,
        }
    return {
        "id": pid,
        "wiring": str(RuntimeWiring.LEGACY),
        "backend_generation": None,
        "native_session_id": None,
        "health": None,
        "settings": None,
        "capabilities": _legacy_capabilities(target),
        "active_turn": None,
        "queued": queued,
    }
