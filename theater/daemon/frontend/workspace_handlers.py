"""Thin public handlers for durable workspace lifecycle operations."""

from __future__ import annotations

from types import MappingProxyType

from theater.daemon.frontend.handshake import ConnectionContext
from theater.daemon.frontend.validation import PublicRequestError
from theater.daemon.worktrees.service import WorkspaceService


def _service(daemon) -> WorkspaceService:
    service = getattr(daemon, "workspace_service", None)
    if not isinstance(service, WorkspaceService):
        raise TypeError("daemon workspace service is not composed")
    return service


async def workspaces_register(
    daemon,
    context: ConnectionContext,
    params: dict,
    *,
    idempotency_key: str,
) -> object:
    return await _service(daemon).register(
        client_id=context.client_id,
        idempotency_key=idempotency_key,
        params=params,
    )


async def workspaces_list(daemon, _context: ConnectionContext, params: dict) -> dict:
    try:
        records, next_cursor = _service(daemon).list(
            cursor=params.get("cursor"),
            limit=params.get("limit", 200),
            state=params.get("state"),
        )
    except ValueError as exc:
        raise PublicRequestError("bad_request", str(exc)) from exc
    service = _service(daemon)
    return {
        "items": [service.project(record) for record in records],
        "next_cursor": next_cursor,
    }


async def workspaces_get(daemon, _context: ConnectionContext, params: dict) -> dict:
    service = _service(daemon)
    return service.project(service.get(params["workspace_id"]))


async def workspaces_cleanup(
    daemon,
    context: ConnectionContext,
    params: dict,
    *,
    idempotency_key: str,
) -> object:
    return _service(daemon).cleanup(
        client_id=context.client_id,
        actor_participant_id=None,
        idempotency_key=idempotency_key,
        params=params,
    )


async def workspaces_prepare_delete(
    daemon,
    context: ConnectionContext,
    params: dict,
    *,
    idempotency_key: str,
) -> object:
    return _service(daemon).prepare_external_delete(
        client_id=context.client_id,
        idempotency_key=idempotency_key,
        params=params,
    )


async def workspaces_confirm_delete(
    daemon,
    context: ConnectionContext,
    params: dict,
    *,
    idempotency_key: str,
) -> object:
    return _service(daemon).confirm_external_delete(
        client_id=context.client_id,
        idempotency_key=idempotency_key,
        params=params,
    )


async def workspaces_cancel_delete(
    daemon,
    context: ConnectionContext,
    params: dict,
    *,
    idempotency_key: str,
) -> object:
    return _service(daemon).cancel_external_delete(
        client_id=context.client_id,
        idempotency_key=idempotency_key,
        params=params,
    )


WORKSPACE_HANDLERS = MappingProxyType(
    {
        "frontend.workspaces.register": workspaces_register,
        "frontend.workspaces.list": workspaces_list,
        "frontend.workspaces.get": workspaces_get,
        "frontend.workspaces.cleanup": workspaces_cleanup,
        "frontend.workspaces.prepare_delete": workspaces_prepare_delete,
        "frontend.workspaces.confirm_delete": workspaces_confirm_delete,
        "frontend.workspaces.cancel_delete": workspaces_cancel_delete,
    }
)

__all__ = ["WORKSPACE_HANDLERS"]
