"""Shared vocabulary of the control mixins: error codes, labels, actions, row helpers."""

from __future__ import annotations

from dataclasses import dataclass

from theater.constants.daemon import CONTROL_AMBIGUOUS_DELIVERY_DEADLINE_SECONDS
from theater.daemon.controls._host import ControlHost
from theater.daemon.controls.routing import ControlRoute
from theater.daemon.persistence.repositories.control_operations import ControlOperation
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    DeliveryResult,
    HarnessRuntime,
    RuntimeCapability,
    RuntimeSnapshot,
)

#: Compatibility export for existing callers.
AMBIGUOUS_DELIVERY_DEADLINE_SECONDS = CONTROL_AMBIGUOUS_DELIVERY_DEADLINE_SECONDS

DAEMON_RESTARTED_ERROR_CODE = "daemon_restarted"
DELIVERY_UNKNOWN_ERROR_CODE = "delivery_unknown"
INTERRUPTED_ERROR_CODE = "interrupted"
CONTROL_TRANSFERRED_ERROR_CODE = "control_transferred"
NATIVE_TURN_CONFLICT_ERROR_CODE = "native_turn_conflict"
SEND_REJECTED_ERROR_CODE = "send_rejected"

#: Delivery outcome labels for the control latency metric.
CONTROL_DELIVERY_ACCEPTED = DeliveryResult.ACCEPTED.value
CONTROL_DELIVERY_REJECTED = DeliveryResult.REJECTED.value
CONTROL_DELIVERY_UNKNOWN = DeliveryResult.UNKNOWN.value
CONTROL_DELIVERY_QUEUED = "queued"

#: Bounded transport label while the original body has not established a transport — every refusal
#: raised before classification, a disconnected native, a legacy path that never reached delivery.
CONTROL_TRANSPORT_UNKNOWN = "unknown"

#: Bounded reasons an unknown delivery is counted.
CONTROL_UNKNOWN_ACK_LOST = "ack_lost"
CONTROL_UNKNOWN_RECEIPT_MISMATCH = "receipt_mismatch"
CONTROL_UNKNOWN_RECEIPT_UNKNOWN = "receipt_unknown"
CONTROL_UNKNOWN_UNCORRELATED = "uncorrelated"
CONTROL_UNKNOWN_READBACK_FAILED = "readback_failed"
CONTROL_UNKNOWN_RESTART = "restart"
CONTROL_UNKNOWN_DEADLINE = "deadline"

#: The actions the authorize gate is asked about.
ACTION_SEND = "send"
ACTION_STEER = "steer"
ACTION_QUEUE_FOLLOWUP = "queue_followup"
ACTION_QUEUE_DISPATCH = "queue_dispatch"
ACTION_SETTINGS_UPDATE = "settings_update"
ACTION_INTERRUPT = "interrupt"
ACTION_TERMINATE = "terminate"
LABEL_INTERRUPTION = "interruption"
LABEL_SETTINGS_UPDATE = "settings update"
LABEL_STEERING = "steering"


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeControlPreparation:
    participant_id: str
    route_capability: RuntimeCapability
    required_capability: RuntimeCapability | None
    action: str
    refusal_label: str
    initial_dispatch: bool = False
    require_available: bool = True


@dataclass(frozen=True, slots=True)
class NativeControlContext:
    runtime: HarnessRuntime
    route: ControlRoute
    snapshot: RuntimeSnapshot


async def prepare_native_control(
    host: ControlHost,
    runtime: HarnessRuntime | None,
    preparation: NativeControlPreparation,
) -> NativeControlContext:
    """Snapshot and fence one native route before its control-specific admission."""
    participant_id = preparation.participant_id
    if runtime is None:
        raise host._disconnected_native_refusal(participant_id, preparation.refusal_label)
    snapshot = await host._snapshot_for_control(
        runtime,
        participant_id,
        initial_dispatch=preparation.initial_dispatch,
    )
    route = host._require_current_native_route(
        participant_id,
        preparation.route_capability,
        snapshot,
        require_available=preparation.require_available,
    )
    if preparation.required_capability is not None:
        host._require_capability(
            participant_id,
            snapshot,
            preparation.required_capability,
            preparation.action,
        )
    return NativeControlContext(runtime, route, snapshot)


def _delivery_label(result: DeliveryResult | None) -> str:
    """Map a settled receipt result to the latency outcome label."""
    return result.value if result is not None else CONTROL_DELIVERY_UNKNOWN


def _error_code_of(exc: Exception) -> str:
    return getattr(exc, "code", None) or "dispatch_failed"


def _operation_row(
    *,
    operation_id: str,
    participant_id: str,
    kind: ControlKind,
    transport: ControlTransport,
    phase: ControlDeliveryPhase,
    created_at: float,
    updated_at: float,
    job_handle: str | None = None,
    backend_generation: int | None = None,
    native_session_id: str | None = None,
    native_turn_id: str | None = None,
    provider_id: str | None = None,
    provider_generation: int | None = None,
    terminal_id: str | None = None,
    terminal_incarnation: str | None = None,
    queue_sequence: int | None = None,
    payload: str | None = None,
) -> ControlOperation:
    return ControlOperation(
        operation_id=operation_id,
        participant_id=participant_id,
        kind=kind,
        transport=transport,
        delivery_phase=phase,
        job_handle=job_handle,
        backend_generation=backend_generation,
        native_session_id=native_session_id,
        native_turn_id=native_turn_id,
        provider_id=provider_id,
        provider_generation=provider_generation,
        terminal_id=terminal_id,
        terminal_incarnation=terminal_incarnation,
        queue_sequence=queue_sequence,
        payload=payload,
        created_at=created_at,
        updated_at=updated_at,
    )
