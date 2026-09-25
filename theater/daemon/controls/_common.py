"""Shared vocabulary of the control mixins: error codes, labels, actions, row helpers."""

from __future__ import annotations

from theater.constants.daemon import CONTROL_AMBIGUOUS_DELIVERY_DEADLINE_SECONDS
from theater.daemon.persistence.repositories.control_operations import ControlOperation
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    DeliveryResult,
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
