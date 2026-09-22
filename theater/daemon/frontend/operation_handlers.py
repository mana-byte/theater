"""Public list/get/wait/reconcile handlers for durable operations."""

from __future__ import annotations

from types import MappingProxyType

from theater.daemon.frontend.handshake import ConnectionContext
from theater.daemon.frontend.validation import PublicRequestError
from theater.daemon.operations import OperationService, operation_to_wire


def _service(daemon) -> OperationService:
    service = getattr(daemon, "operation_service", None)
    if not isinstance(service, OperationService):
        raise TypeError("daemon operation service is not composed")
    return service


async def operations_list(daemon, _context: ConnectionContext, params: dict) -> dict:
    try:
        records, next_cursor = _service(daemon).list(
            cursor=params.get("cursor"),
            limit=params.get("limit", 200),
            unsettled_only=params.get("unsettled_only", False),
            target_id=params.get("target_id"),
        )
    except ValueError as exc:
        raise PublicRequestError("bad_request", str(exc)) from exc
    return {
        "items": [operation_to_wire(record) for record in records],
        "next_cursor": next_cursor,
    }


async def operations_get(daemon, _context: ConnectionContext, params: dict) -> dict:
    return operation_to_wire(_service(daemon).get(params["operation_id"]))


async def operations_await(daemon, _context: ConnectionContext, params: dict) -> dict:
    record, timed_out = await _service(daemon).wait(
        params["operation_id"], wait_seconds=params.get("wait_seconds", 25.0)
    )
    return {"operation": operation_to_wire(record), "timed_out": timed_out}


async def operations_reconcile(daemon, _context: ConnectionContext, params: dict) -> dict:
    return operation_to_wire(await _service(daemon).reconcile(params["operation_id"]))


OPERATION_HANDLERS = MappingProxyType(
    {
        "frontend.operations.list": operations_list,
        "frontend.operations.get": operations_get,
        "frontend.operations.await": operations_await,
        "frontend.operations.reconcile": operations_reconcile,
    }
)

__all__ = ["OPERATION_HANDLERS"]
