"""Curated bootstrap, health, and initial safe-read public handlers."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from theater import __version__
from theater.daemon.frontend.handshake import ConnectionContext, daemon_instance_id
from theater.daemon.frontend.operation_handlers import OPERATION_HANDLERS
from theater.daemon.frontend.provider_handlers import PROVIDER_HANDLERS
from theater.daemon.frontend.scratchpad_handlers import SCRATCHPAD_HANDLERS
from theater.daemon.frontend.validation import PublicRequestError
from theater.daemon.frontend.workspace_handlers import WORKSPACE_HANDLERS
from theater.frontend.capabilities import (
    CALLBACK_CATALOG,
    CAPABILITIES,
    METHOD_CATALOG,
    PUBLIC_API_MAJOR,
    PUBLIC_API_MINOR,
    PUBLIC_LIMITS,
)
from theater.frontend.schemas import load_schema_resources


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


async def contract_get(_daemon, _context: ConnectionContext, _params: dict) -> dict:
    methods = {
        name: {
            "class": spec.method_class.value,
            "roles": sorted(role.value for role in spec.roles),
            "request_schema_id": spec.request_schema_id,
            "response_schema_id": spec.response_schema_id,
            "params_schema_id": spec.params_schema_id,
            "result_schema_id": spec.result_schema_id,
            "idempotency_required": spec.idempotency_required,
            "required_capabilities": sorted(spec.required_capabilities),
        }
        for name, spec in METHOD_CATALOG.items()
    }
    callbacks = {
        name: {
            "request_schema_id": spec.request_schema_id,
            "response_schema_id": spec.response_schema_id,
            "params_schema_id": spec.params_schema_id,
            "result_schema_id": spec.result_schema_id,
            "mutating": spec.mutating,
        }
        for name, spec in CALLBACK_CATALOG.items()
    }
    return {
        "api": {"major": PUBLIC_API_MAJOR, "minor": PUBLIC_API_MINOR},
        "capabilities": list(CAPABILITIES),
        "limits": dict(PUBLIC_LIMITS),
        "methods": methods,
        "callbacks": callbacks,
    }


async def schemas_get(_daemon, _context: ConnectionContext, _params: dict) -> dict:
    return {
        "resources": {
            uri: _json_value(resource) for uri, resource in load_schema_resources().items()
        }
    }


async def health_get(daemon, _context: ConnectionContext, _params: dict) -> dict:
    stopping = getattr(daemon, "_stopping", None)
    return {
        "status": "stopping" if stopping is not None and stopping.is_set() else "ok",
        "api": {"major": PUBLIC_API_MAJOR, "minor": PUBLIC_API_MINOR},
        "daemon_instance_id": daemon_instance_id(daemon),
        "package_version": __version__,
    }


def _participant_projection(daemon, participant) -> dict[str, Any]:
    origin = participant.origin.value if participant.origin is not None else participant.tier.value
    owner_kind = (
        participant.control_owner_kind.value
        if participant.control_owner_kind is not None
        else "local_operator"
    )
    owner: dict[str, object] = {
        "kind": owner_kind,
        "revision": participant.control_revision,
    }
    if owner_kind == "participant":
        owner["participant_id"] = participant.control_owner_id

    snapshot = daemon.presence.snapshot(participant.id)
    result: dict[str, Any] = {
        "participant_id": participant.id,
        "origin": origin,
        "harness": participant.harness,
        "status": participant.status.value,
        "owner": owner,
        # Wave 03 has no authenticated provider/native public route service yet.
        "addressable": False,
        "presence": snapshot.state.value,
        "actions": {},
    }
    optional = {
        "parent_id": participant.parent_id,
        "cwd": participant.cwd,
        "workspace_id": participant.workspace_id,
        "name": participant.name,
        "description": participant.description,
    }
    result.update(optional)
    return result


async def participants_get(daemon, _context: ConnectionContext, params: dict) -> dict:
    participant_id = params["participant_id"]
    participant = daemon.registry.get(participant_id)
    if participant is None:
        raise PublicRequestError(
            "not_found", f"no participant {participant_id!r}", {"participant_id": participant_id}
        )
    return _participant_projection(daemon, participant)


PUBLIC_HANDLERS = MappingProxyType(
    {
        "frontend.contract.get": contract_get,
        "frontend.schemas.get": schemas_get,
        "frontend.health.get": health_get,
        "frontend.participants.get": participants_get,
        **OPERATION_HANDLERS,
        **PROVIDER_HANDLERS,
        **SCRATCHPAD_HANDLERS,
        **WORKSPACE_HANDLERS,
    }
)

__all__ = ["PUBLIC_HANDLERS"]
