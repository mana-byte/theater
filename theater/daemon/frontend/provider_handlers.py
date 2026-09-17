"""Curated public handlers for terminal-provider administration and reports."""

from __future__ import annotations

from types import MappingProxyType

from theater.daemon.frontend.handshake import ConnectionContext
from theater.daemon.frontend.validation import PublicRequestError
from theater.daemon.terminals import TerminalProviderService


def _service(daemon) -> TerminalProviderService:
    service = getattr(daemon, "terminal_service", None)
    if not isinstance(service, TerminalProviderService):
        raise TypeError("daemon terminal-provider service is not composed")
    return service


async def providers_register(
    daemon,
    context: ConnectionContext,
    params: dict,
    *,
    idempotency_key: str,
) -> object:
    return _service(daemon).registry.register(
        client_id=context.client_id,
        idempotency_key=idempotency_key,
        params=params,
    )


async def providers_update(
    daemon,
    context: ConnectionContext,
    params: dict,
    *,
    idempotency_key: str,
) -> object:
    return _service(daemon).registry.update(
        client_id=context.client_id,
        idempotency_key=idempotency_key,
        params=params,
    )


async def providers_list(daemon, _context: ConnectionContext, params: dict) -> dict:
    service = _service(daemon)
    records, next_cursor = service.registry.list(
        cursor=params.get("cursor"), limit=params.get("limit", 200)
    )
    return {
        "items": [service.registry.project(record) for record in records],
        "next_cursor": next_cursor,
    }


async def providers_get(daemon, _context: ConnectionContext, params: dict) -> dict:
    service = _service(daemon)
    return service.registry.project(service.registry.get(params["provider_id"]))


def _provider_context(context: ConnectionContext) -> tuple[str, int]:
    if context.provider_id is None or context.provider_generation is None:
        raise PublicRequestError(
            "wrong_connection_role", "provider reports require an authenticated provider"
        )
    return context.provider_id, context.provider_generation


async def providers_heartbeat(daemon, context: ConnectionContext, params: dict) -> dict:
    provider_id, generation = _provider_context(context)
    if params["provider_generation"] != generation:
        raise PublicRequestError(
            "stale_generation",
            "the report generation does not match this provider connection",
            {"provider_id": provider_id, "provider_generation": params["provider_generation"]},
        )
    return _service(daemon).heartbeat(provider_id, generation, params["report_revision"])


async def providers_report(daemon, context: ConnectionContext, params: dict) -> dict:
    provider_id, generation = _provider_context(context)
    if params["provider_generation"] != generation:
        raise PublicRequestError(
            "stale_generation",
            "the report generation does not match this provider connection",
            {"provider_id": provider_id, "provider_generation": params["provider_generation"]},
        )
    return _service(daemon).report(
        provider_id,
        generation,
        params["report_revision"],
        params.get("facts"),
    )


async def provider_terminals_list(daemon, _context: ConnectionContext, params: dict) -> dict:
    service = _service(daemon)
    provider = service.registry.get(params["provider_id"])
    if params.get("refresh", False):
        result = await service.inventory(
            provider.provider_id,
            provider.generation,
            cursor=params.get("cursor"),
            limit=params.get("limit", 200),
        )
        terminals = result["terminals"]
        if not isinstance(terminals, list):
            raise TypeError("validated provider inventory terminals must be an array")
        return {
            "items": terminals,
            "next_cursor": result.get("next_cursor"),
            "complete": result["complete"],
            "provider_generation": result["provider_generation"],
            "report_revision": result["report_revision"],
        }
    items = [
        service.binding_projection(binding)
        for binding in service.bindings.list(provider.provider_id)
    ]
    limit = params.get("limit", 200)
    cursor = params.get("cursor")
    start = 0
    if cursor is not None:
        for index, item in enumerate(items):
            if item["participant_id"] == cursor:
                start = index + 1
                break
        else:
            raise PublicRequestError("bad_request", f"unknown terminal cursor {cursor!r}")
    page = items[start : start + limit]
    next_cursor = page[-1]["participant_id"] if start + limit < len(items) and page else None
    return {"items": page, "next_cursor": next_cursor}


async def provider_terminals_inspect(daemon, _context: ConnectionContext, params: dict) -> object:
    service = _service(daemon)
    provider = service.registry.get(params["provider_id"])
    return await service.inspect(
        provider.provider_id,
        provider.generation,
        params["terminal_id"],
        params["terminal_incarnation"],
    )


PROVIDER_HANDLERS = MappingProxyType(
    {
        "frontend.providers.register": providers_register,
        "frontend.providers.update": providers_update,
        "frontend.providers.list": providers_list,
        "frontend.providers.get": providers_get,
        "frontend.providers.heartbeat": providers_heartbeat,
        "frontend.providers.report": providers_report,
        "frontend.providers.terminals.list": provider_terminals_list,
        "frontend.providers.terminals.inspect": provider_terminals_inspect,
    }
)

__all__ = ["PROVIDER_HANDLERS"]
