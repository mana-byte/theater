"""A provider-terminal control operation row for queued-followup tests."""

from __future__ import annotations

from theater.daemon.persistence.repositories.control_operations import ControlOperation
from theater.harness.contracts.runtime import (
    ControlTransport,
)
from theater.models import now


def provider_terminal_op(operation_id, pid, *, kind, phase, job_handle, result=None):
    return ControlOperation(
        operation_id=operation_id,
        participant_id=pid,
        kind=kind,
        transport=ControlTransport.PROVIDER_TERMINAL,
        delivery_phase=phase,
        delivery_result=result,
        job_handle=job_handle,
        created_at=now(),
        updated_at=now(),
    )
