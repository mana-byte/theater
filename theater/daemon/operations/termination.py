"""Durable termination identity shared by private RPCs and public operations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from theater.daemon.operations.service import DispatchIntent, PreparedOperation
from theater.models import PublicOperationRecord, new_id, now


def termination_dispatch(daemon, participant_id: str) -> DispatchIntent:
    """Capture every execution surface before a termination can be transmitted."""
    terminal = daemon.controls.terminal_route_for(participant_id).terminal
    runtime = daemon.store.get_runtime_binding(participant_id)
    native = runtime if runtime is not None and runtime.native_session_id is not None else None
    return DispatchIntent(
        phase="termination_preparing",
        provider_id=None if terminal is None else terminal.provider_id,
        provider_generation=None if terminal is None else terminal.provider_generation,
        terminal_id=None if terminal is None else terminal.terminal_id,
        terminal_incarnation=None if terminal is None else terminal.terminal_incarnation,
        occupant_evidence=None if terminal is None else terminal.occupant_evidence,
        process_facts=None if terminal is None else terminal.process_facts,
        backend_generation=None if native is None else native.backend_generation,
        native_session_id=None if native is None else native.native_session_id,
        composite_termination=terminal is not None and native is not None,
    )


async def terminate_with_operation(
    daemon,
    participant_id: str,
    *,
    caller_id: str,
    execute: Callable[[str], Awaitable[dict]],
) -> dict:
    """Keep the synchronous private RPC's result and errors, with durable callback identity."""
    service = daemon.operation_service
    dispatch = termination_dispatch(daemon, participant_id)
    client_id = "private-kill"

    def prepare(operation_id, _unit):
        timestamp = now()
        return PreparedOperation(
            record=PublicOperationRecord(
                operation_id=operation_id,
                kind="participants.terminate",
                actor_client_id=client_id,
                actor_participant_id=None if caller_id == "cli" else caller_id,
                target_ids=(participant_id,),
                state="accepted",
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

    accepted = service.accept_operation(
        client_id=client_id,
        idempotency_key=new_id(),
        method="frontend.participants.terminate",
        params={"participant_id": participant_id},
        prepare=prepare,
    )
    operation_id = accepted.record.operation_id
    service.mark_dispatch_intent(operation_id, dispatch)
    try:
        result = await execute(operation_id)
    except asyncio.CancelledError:
        service.mark_uncertain(
            operation_id,
            phase="termination_cancelled",
            error={"code": "provider_unavailable", "message": "termination outcome is unknown"},
        )
        raise
    except Exception as exc:
        from theater.daemon.frontend.mutation_errors import operation_error

        error = operation_error(exc, default_code="termination_failed")
        details = error.get("details")
        if isinstance(details, dict) and details.get("possibly_executed") is True:
            service.mark_uncertain(operation_id, phase="exit_unverified", error=error)
        else:
            service.fail(operation_id, phase="termination_refused", error=error)
        raise
    service.succeed(operation_id, phase="exit_verified", result=result)
    return result
