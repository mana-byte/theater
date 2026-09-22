"""Public frontend adapters for the shared global scratchpad service."""

from __future__ import annotations

from types import MappingProxyType

from theater.daemon.frontend.handshake import ConnectionContext
from theater.daemon.frontend.validation import PublicRequestError
from theater.daemon.operations import OperationService
from theater.daemon.scratchpad import ScratchpadService, service_for_daemon


def _service(daemon) -> ScratchpadService:
    return service_for_daemon(daemon)


def _operations(daemon) -> OperationService:
    service = getattr(daemon, "operation_service", None)
    if not isinstance(service, OperationService):
        raise TypeError("daemon operation service is not composed")
    return service


async def scratchpad_namespaces(daemon, _context: ConnectionContext, params: dict) -> dict:
    page = _service(daemon).namespaces(
        after_namespace=params.get("cursor"), limit=params.get("limit", 200)
    )
    return {"items": list(page.namespaces), "next_cursor": page.next_cursor}


async def scratchpad_get(daemon, _context: ConnectionContext, params: dict) -> dict:
    page = _service(daemon).get(
        namespace=params["namespace"],
        keys=params.get("keys"),
        after_key=params.get("cursor"),
        limit=params.get("limit", 200),
    )
    response: dict[str, object] = {
        "items": [{"key": key, "value": value} for key, value in page.entries.items()],
        "next_cursor": page.after_key,
        "truncated": page.truncated,
    }
    if page.oversized_bytes:
        response.update(
            {
                "oversized_key": page.oversized_key,
                "oversized_digest": page.oversized_digest,
                "oversized_bytes": page.oversized_bytes,
            }
        )
    return response


async def scratchpad_write(
    daemon,
    context: ConnectionContext,
    params: dict,
    *,
    idempotency_key: str,
) -> object:
    service = _service(daemon)

    def action(unit) -> object:
        key = service.write(
            namespace=params["namespace"],
            value=params["value"],
            key=params.get("key"),
            actor_client_id=context.client_id,
            connection=unit.connection,
        )
        return {"namespace": params["namespace"], "key": key}

    return (
        _operations(daemon)
        .execute_idempotent(
            client_id=context.client_id,
            idempotency_key=idempotency_key,
            method="frontend.scratchpad.write",
            params=params,
            action=action,
        )
        .value
    )


async def scratchpad_delete(
    daemon,
    context: ConnectionContext,
    params: dict,
    *,
    idempotency_key: str,
) -> object:
    keys = params.get("keys", [])
    clear = params.get("clear", False)
    if clear and keys:
        raise PublicRequestError(
            "bad_request", "scratchpad.delete 'clear' cannot be combined with named keys"
        )
    if not clear and not keys:
        raise PublicRequestError("bad_request", "scratchpad.delete needs keys or clear=true")
    service = _service(daemon)

    def action(unit) -> object:
        if clear:
            return {
                "namespace": params["namespace"],
                "deleted_count": service.clear(
                    namespace=params["namespace"], connection=unit.connection
                ),
            }
        return {
            "namespace": params["namespace"],
            "deleted": service.delete(
                namespace=params["namespace"], keys=keys, connection=unit.connection
            ),
        }

    return (
        _operations(daemon)
        .execute_idempotent(
            client_id=context.client_id,
            idempotency_key=idempotency_key,
            method="frontend.scratchpad.delete",
            params=params,
            action=action,
        )
        .value
    )


SCRATCHPAD_HANDLERS = MappingProxyType(
    {
        "frontend.scratchpad.namespaces": scratchpad_namespaces,
        "frontend.scratchpad.get": scratchpad_get,
        "frontend.scratchpad.write": scratchpad_write,
        "frontend.scratchpad.delete": scratchpad_delete,
    }
)

__all__ = ["SCRATCHPAD_HANDLERS"]
