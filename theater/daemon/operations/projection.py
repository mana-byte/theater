"""Stable public projections for persisted operation records."""

from __future__ import annotations

from theater.models import PublicOperationRecord


def operation_to_wire(record: PublicOperationRecord) -> dict[str, object]:
    actor: dict[str, object] = {"client_id": record.actor_client_id}
    if record.actor_participant_id is not None:
        actor["participant_id"] = record.actor_participant_id

    dispatch = _dispatch_identity(record)
    value: dict[str, object] = {
        "operation_id": record.operation_id,
        "kind": record.kind,
        "state": record.state,
        "phase": record.phase,
        "actor": actor,
        "target_ids": list(record.target_ids),
        "control_operation_id": record.control_operation_id,
        "job_handle": record.job_handle,
        "dispatch_identity": dispatch,
        "result": record.result,
        "error": record.error,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }
    if record.settled_at is not None:
        value["settled_at"] = record.settled_at
    return value


def operation_event_payload(record: PublicOperationRecord) -> dict[str, object]:
    """Publish the complete bounded operation projection for an upsert event."""
    return operation_to_wire(record)


def _dispatch_identity(record: PublicOperationRecord) -> dict[str, object] | None:
    values: dict[str, object] = {
        "provider_id": record.dispatch_provider_id,
        "provider_generation": record.dispatch_provider_generation,
        "terminal_id": record.dispatch_terminal_id,
        "terminal_incarnation": record.dispatch_terminal_incarnation,
        "backend_generation": record.dispatch_backend_generation,
        "native_session_id": record.dispatch_native_session_id,
        "native_turn_id": record.dispatch_native_turn_id,
    }
    return values if any(value is not None for value in values.values()) else None


__all__ = ["operation_event_payload", "operation_to_wire"]
