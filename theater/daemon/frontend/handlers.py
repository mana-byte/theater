"""Curated bootstrap, health, and initial safe-read public handlers."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from theater import __version__
from theater.daemon.frontend.catalog_handlers import CATALOG_HANDLERS
from theater.daemon.frontend.control_handlers import CONTROL_HANDLERS
from theater.daemon.frontend.diagnostic_handlers import DIAGNOSTIC_HANDLERS
from theater.daemon.frontend.handshake import ConnectionContext, daemon_instance_id
from theater.daemon.frontend.job_handlers import JOB_HANDLERS
from theater.daemon.frontend.observation_handlers import OBSERVATION_HANDLERS
from theater.daemon.frontend.operation_handlers import OPERATION_HANDLERS
from theater.daemon.frontend.participant_handlers import PARTICIPANT_HANDLERS
from theater.daemon.frontend.participant_mutation_handlers import PARTICIPANT_MUTATION_HANDLERS
from theater.daemon.frontend.participant_read_handlers import PARTICIPANT_READ_HANDLERS
from theater.daemon.frontend.provider_handlers import PROVIDER_HANDLERS
from theater.daemon.frontend.scratchpad_handlers import SCRATCHPAD_HANDLERS
from theater.daemon.frontend.state_handlers import STATE_HANDLERS
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


PUBLIC_HANDLERS = MappingProxyType(
    {
        "frontend.contract.get": contract_get,
        "frontend.schemas.get": schemas_get,
        "frontend.health.get": health_get,
        **STATE_HANDLERS,
        **PARTICIPANT_HANDLERS,
        **PARTICIPANT_MUTATION_HANDLERS,
        **PARTICIPANT_READ_HANDLERS,
        **CONTROL_HANDLERS,
        **JOB_HANDLERS,
        **OBSERVATION_HANDLERS,
        **OPERATION_HANDLERS,
        **PROVIDER_HANDLERS,
        **SCRATCHPAD_HANDLERS,
        **WORKSPACE_HANDLERS,
        **CATALOG_HANDLERS,
        **DIAGNOSTIC_HANDLERS,
    }
)

__all__ = ["PUBLIC_HANDLERS"]
