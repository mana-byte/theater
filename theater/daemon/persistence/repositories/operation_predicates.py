"""Distinct delivery questions over persisted control-operation facts."""

from __future__ import annotations

from collections.abc import Collection
from typing import TYPE_CHECKING

from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    DeliveryResult,
)

if TYPE_CHECKING:
    from theater.daemon.persistence.repositories.control_operations import ControlOperation


def may_have_reached_target(operation: ControlOperation) -> bool:
    """A dispatched prompt or a settled result other than rejection may need Git evidence."""
    return operation.delivery_phase is ControlDeliveryPhase.DISPATCHED or (
        operation.delivery_phase is ControlDeliveryPhase.SETTLED
        and operation.delivery_result is not DeliveryResult.REJECTED
    )


def started_task(operation: ControlOperation) -> bool:
    """A named participant's work includes uncertain receipts and named native turns."""
    return (
        operation.delivery_phase is ControlDeliveryPhase.DISPATCHED
        or operation.delivery_result in (DeliveryResult.ACCEPTED, DeliveryResult.UNKNOWN)
        or operation.native_turn_id is not None
    )


def proven_unsent(operation: ControlOperation, error_codes: Collection[str]) -> bool:
    """A rejected row is safe to release only without a turn or execution barrier."""
    return (
        operation.delivery_phase is ControlDeliveryPhase.SETTLED
        and operation.delivery_result is DeliveryResult.REJECTED
        and operation.error_code in error_codes
        and operation.native_turn_id is None
        and not operation.execution_barrier
    )


def cancel_delivery_started(operation: ControlOperation) -> bool:
    """Cancellation treats even a settled rejection as past the queue boundary."""
    return operation.delivery_phase in (
        ControlDeliveryPhase.DISPATCHED,
        ControlDeliveryPhase.SETTLED,
    )


def cancel_outcome_definitive(operation: ControlOperation) -> bool:
    return (
        operation.delivery_phase is ControlDeliveryPhase.SETTLED
        and operation.delivery_result in (DeliveryResult.ACCEPTED, DeliveryResult.REJECTED)
        and not operation.execution_barrier
    )


def cancel_accepted(operation: ControlOperation) -> bool:
    return (
        operation.delivery_phase is ControlDeliveryPhase.SETTLED
        and operation.delivery_result is DeliveryResult.ACCEPTED
        and not operation.execution_barrier
    )


def accepted_for_play_assignment(operation: ControlOperation) -> bool:
    """The play's accepted receipt needs no phase or barrier inference."""
    return operation.delivery_result is DeliveryResult.ACCEPTED


def names_exact_native_prompt_turn(operation: ControlOperation) -> bool:
    """Only a fully identified native prompt turn can prove play delivery."""
    return (
        operation.kind in (ControlKind.SEND, ControlKind.QUEUE_FOLLOWUP)
        and operation.transport is ControlTransport.NATIVE_RUNTIME
        and operation.backend_generation is not None
        and operation.native_session_id is not None
        and operation.native_turn_id is not None
    )
