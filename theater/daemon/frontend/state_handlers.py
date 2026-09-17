"""Public immutable state pages and durable journal follow handlers."""

from __future__ import annotations

from types import MappingProxyType

from theater.daemon.events import StateService
from theater.daemon.events.reader import StateReadError
from theater.daemon.frontend.handshake import ConnectionContext
from theater.daemon.frontend.validation import PublicRequestError
from theater.frontend.capabilities import METHOD_CATALOG, PUBLIC_LIMITS
from theater.frontend.schemas import validator_for


def _service(daemon) -> StateService:
    service = getattr(daemon, "state_service", None)
    if not isinstance(service, StateService):
        raise TypeError("daemon state service is not composed")
    return service


def _validated(method: str, result: dict[str, object]) -> dict[str, object]:
    validator_for(METHOD_CATALOG[method].result_schema_id).validate(result)
    return result


def _public_error(error: StateReadError) -> PublicRequestError:
    return PublicRequestError(error.code, error.message, error.details)


async def state_snapshot(daemon, context: ConnectionContext, params: dict) -> dict[str, object]:
    try:
        return _validated(
            "frontend.state.snapshot",
            _service(daemon).snapshot(
                context.client_id,
                page_size=params.get("page_size", int(PUBLIC_LIMITS["entity_page_default"])),
            ),
        )
    except StateReadError as exc:
        raise _public_error(exc) from exc


async def state_page(daemon, context: ConnectionContext, params: dict) -> dict[str, object]:
    try:
        return _validated(
            "frontend.state.page",
            _service(daemon).page(context.client_id, params["snapshot_id"], params["page"]),
        )
    except StateReadError as exc:
        raise _public_error(exc) from exc


async def state_release(daemon, context: ConnectionContext, params: dict) -> dict[str, object]:
    try:
        _service(daemon).release(context.client_id, params["snapshot_id"])
    except StateReadError as exc:
        raise _public_error(exc) from exc
    return _validated("frontend.state.release", {"released": True})


async def state_follow(daemon, _context: ConnectionContext, params: dict) -> dict[str, object]:
    try:
        return _validated(
            "frontend.state.follow",
            await _service(daemon).follow(
                params["cursor"],
                wait_seconds=params.get("wait_seconds", PUBLIC_LIMITS["follow_wait_seconds"]),
                limit=params.get("limit", PUBLIC_LIMITS["entity_page_default"]),
            ),
        )
    except StateReadError as exc:
        raise _public_error(exc) from exc


STATE_HANDLERS = MappingProxyType(
    {
        "frontend.state.snapshot": state_snapshot,
        "frontend.state.page": state_page,
        "frontend.state.release": state_release,
        "frontend.state.follow": state_follow,
    }
)

__all__ = ["STATE_HANDLERS"]
