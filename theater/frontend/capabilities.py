"""Frozen RC10 capability, method, and callback catalogs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

PUBLIC_API_MAJOR = 1
PUBLIC_API_MINOR = 0
MAX_EXACT_JSON_INTEGER = 9_007_199_254_740_991
MAX_FRAME_BYTES = 64 * 1024 * 1024


class ConnectionRole(StrEnum):
    OPERATOR = "operator"
    PROVIDER = "provider"


class ConnectionChannel(StrEnum):
    RPC = "rpc"
    CALLBACK = "callback"


class MethodClass(StrEnum):
    BOOTSTRAP = "bootstrap"
    READ = "read"
    WRITE = "write"
    OPERATION = "operation"
    OPERATION_REFERENCE = "operation_reference"
    PROVIDER_REPORT = "provider_report"


@dataclass(frozen=True, slots=True)
class MethodSpec:
    name: str
    method_class: MethodClass
    roles: frozenset[ConnectionRole]
    request_schema_id: str
    response_schema_id: str
    params_schema_id: str
    result_schema_id: str
    idempotency_required: bool
    required_capabilities: frozenset[str]


@dataclass(frozen=True, slots=True)
class CallbackSpec:
    name: str
    request_schema_id: str
    response_schema_id: str
    params_schema_id: str
    result_schema_id: str
    mutating: bool


CONTRACT_CAPABILITY = "contract.v1"
ORCHESTRATION_CAPABILITY = "orchestration.v1"
STATE_FOLLOW_CAPABILITY = "state.follow.v1"
TERMINAL_PROVIDER_CAPABILITY = "terminal-provider.v1"
WORKSPACE_CAPABILITY = "workspaces.v1"
SCRATCHPAD_CAPABILITY = "scratchpad.v1"
CATALOG_CAPABILITY = "catalogs.v1"
OBSERVATION_CAPABILITY = "observation.v1"
TRAJECTORY_CAPABILITY = "trajectory.v1"
DIAGNOSTICS_CAPABILITY = "diagnostics.v1"

CAPABILITIES = (
    CONTRACT_CAPABILITY,
    ORCHESTRATION_CAPABILITY,
    STATE_FOLLOW_CAPABILITY,
    TERMINAL_PROVIDER_CAPABILITY,
    WORKSPACE_CAPABILITY,
    SCRATCHPAD_CAPABILITY,
    CATALOG_CAPABILITY,
    OBSERVATION_CAPABILITY,
    TRAJECTORY_CAPABILITY,
    DIAGNOSTICS_CAPABILITY,
)

PUBLIC_LIMITS: Mapping[str, int] = MappingProxyType(
    {
        "max_frame_bytes": MAX_FRAME_BYTES,
        "max_in_flight": 1,
        "entity_page_default": 200,
        "entity_page_max": 500,
        "snapshot_lifetime_seconds": 60,
        "snapshots_per_client": 2,
        "snapshot_cache_bytes": 64 * 1024 * 1024,
        "follow_wait_seconds": 25,
        "follow_wait_max_seconds": 30,
        "handshake_timeout_seconds": 10,
        "provider_heartbeat_seconds": 10,
        "provider_lease_seconds": 30,
        "provider_callback_timeout_seconds": 30,
        "provider_pending_callbacks": 32,
        "provider_mutations_per_terminal": 1,
    }
)

_OPERATOR = frozenset({ConnectionRole.OPERATOR})
_PROVIDER = frozenset({ConnectionRole.PROVIDER})
_BOTH = frozenset({ConnectionRole.OPERATOR, ConnectionRole.PROVIDER})
_METHOD_SCHEMA_ROOT = "https://theater.dev/schemas/frontend/1.0/methods.json#/$defs/"
_ENVELOPE_SCHEMA_ROOT = "https://theater.dev/schemas/frontend/1.0/envelopes.json#/$defs/"
_CALLBACK_SCHEMA_ROOT = "https://theater.dev/schemas/frontend/1.0/callbacks.json#/$defs/"


def _token(name: str) -> str:
    return name.removeprefix("frontend.").replace(".", "_")


def _capability(name: str) -> str:
    domain = name.removeprefix("frontend.").split(".", 1)[0]
    if domain == "state":
        return STATE_FOLLOW_CAPABILITY
    if domain == "providers":
        return TERMINAL_PROVIDER_CAPABILITY
    if domain == "workspaces":
        return WORKSPACE_CAPABILITY
    if domain == "scratchpad":
        return SCRATCHPAD_CAPABILITY
    if domain in {"catalogs", "skills"}:
        return CATALOG_CAPABILITY
    if domain in {"transcripts", "recall"}:
        return OBSERVATION_CAPABILITY
    if domain == "trajectory":
        return TRAJECTORY_CAPABILITY
    if domain in {"usage", "stats", "bus"}:
        return DIAGNOSTICS_CAPABILITY
    if domain in {"contract", "schemas", "health"}:
        return CONTRACT_CAPABILITY
    return ORCHESTRATION_CAPABILITY


def _method(
    name: str,
    method_class: MethodClass,
    *,
    roles: frozenset[ConnectionRole] = _OPERATOR,
) -> MethodSpec:
    token = _token(name)
    return MethodSpec(
        name=name,
        method_class=method_class,
        roles=roles,
        request_schema_id=f"{_ENVELOPE_SCHEMA_ROOT}ordinaryRequest",
        response_schema_id=f"{_ENVELOPE_SCHEMA_ROOT}response",
        params_schema_id=f"{_METHOD_SCHEMA_ROOT}{token}Params",
        result_schema_id=f"{_METHOD_SCHEMA_ROOT}{token}Result",
        idempotency_required=method_class in {MethodClass.WRITE, MethodClass.OPERATION},
        required_capabilities=frozenset({_capability(name)}),
    )


_METHODS = (
    MethodSpec(
        name="frontend.handshake",
        method_class=MethodClass.BOOTSTRAP,
        roles=_BOTH,
        request_schema_id=f"{_ENVELOPE_SCHEMA_ROOT}handshakeRequest",
        response_schema_id=f"{_ENVELOPE_SCHEMA_ROOT}response",
        params_schema_id=f"{_ENVELOPE_SCHEMA_ROOT}handshakeParams",
        result_schema_id=f"{_ENVELOPE_SCHEMA_ROOT}handshakeResult",
        idempotency_required=False,
        required_capabilities=frozenset(),
    ),
    _method("frontend.contract.get", MethodClass.READ, roles=_BOTH),
    _method("frontend.schemas.get", MethodClass.READ, roles=_BOTH),
    _method("frontend.health.get", MethodClass.READ, roles=_BOTH),
    _method("frontend.state.snapshot", MethodClass.READ),
    _method("frontend.state.page", MethodClass.READ),
    _method("frontend.state.release", MethodClass.READ),
    _method("frontend.state.follow", MethodClass.READ),
    _method("frontend.participants.list", MethodClass.READ),
    _method("frontend.participants.get", MethodClass.READ),
    _method("frontend.participants.tree", MethodClass.READ),
    _method("frontend.participants.spawn", MethodClass.OPERATION),
    _method("frontend.participants.adopt", MethodClass.OPERATION),
    _method("frontend.participants.update", MethodClass.WRITE),
    _method("frontend.participants.status", MethodClass.WRITE),
    _method("frontend.participants.terminate", MethodClass.OPERATION),
    _method("frontend.participants.transfer_control", MethodClass.WRITE),
    _method("frontend.controls.get", MethodClass.READ),
    _method("frontend.controls.send", MethodClass.OPERATION),
    _method("frontend.controls.steer", MethodClass.OPERATION),
    _method("frontend.controls.queue_followup", MethodClass.OPERATION),
    _method("frontend.controls.interrupt", MethodClass.OPERATION),
    _method("frontend.controls.settings.update", MethodClass.OPERATION),
    _method("frontend.operations.list", MethodClass.READ),
    _method("frontend.operations.get", MethodClass.READ),
    _method("frontend.operations.await", MethodClass.READ),
    _method("frontend.operations.reconcile", MethodClass.OPERATION_REFERENCE),
    _method("frontend.jobs.list", MethodClass.READ),
    _method("frontend.jobs.get", MethodClass.READ),
    _method("frontend.jobs.await", MethodClass.READ),
    _method("frontend.providers.register", MethodClass.WRITE),
    _method("frontend.providers.update", MethodClass.WRITE),
    _method("frontend.providers.list", MethodClass.READ),
    _method("frontend.providers.get", MethodClass.READ),
    _method("frontend.providers.heartbeat", MethodClass.PROVIDER_REPORT, roles=_PROVIDER),
    _method("frontend.providers.report", MethodClass.PROVIDER_REPORT, roles=_PROVIDER),
    _method("frontend.providers.terminals.list", MethodClass.READ),
    _method("frontend.providers.terminals.inspect", MethodClass.READ),
    _method("frontend.workspaces.register", MethodClass.WRITE),
    _method("frontend.workspaces.list", MethodClass.READ),
    _method("frontend.workspaces.get", MethodClass.READ),
    _method("frontend.workspaces.cleanup", MethodClass.OPERATION),
    _method("frontend.workspaces.prepare_delete", MethodClass.WRITE),
    _method("frontend.workspaces.confirm_delete", MethodClass.WRITE),
    _method("frontend.workspaces.cancel_delete", MethodClass.WRITE),
    _method("frontend.scratchpad.namespaces", MethodClass.READ),
    _method("frontend.scratchpad.get", MethodClass.READ),
    _method("frontend.scratchpad.write", MethodClass.WRITE),
    _method("frontend.scratchpad.delete", MethodClass.WRITE),
    _method("frontend.catalogs.harnesses", MethodClass.READ),
    _method("frontend.catalogs.models", MethodClass.READ),
    _method("frontend.skills.list", MethodClass.READ),
    _method("frontend.skills.load", MethodClass.READ),
    _method("frontend.transcripts.read", MethodClass.READ),
    _method("frontend.transcripts.candidates", MethodClass.READ),
    _method("frontend.transcripts.bind", MethodClass.WRITE),
    _method("frontend.recall.query", MethodClass.READ),
    _method("frontend.recall.read", MethodClass.READ),
    _method("frontend.trajectory.snapshot", MethodClass.READ),
    _method("frontend.trajectory.follow", MethodClass.READ),
    _method("frontend.trajectory.close", MethodClass.READ),
    _method("frontend.trajectory.locate", MethodClass.READ),
    _method("frontend.trajectory.search", MethodClass.READ),
    _method("frontend.usage.totals", MethodClass.READ),
    _method("frontend.usage.summary", MethodClass.READ),
    _method("frontend.usage.by_harness", MethodClass.READ),
    _method("frontend.stats.get", MethodClass.READ),
    _method("frontend.bus.tail", MethodClass.READ),
)

METHOD_CATALOG: Mapping[str, MethodSpec] = MappingProxyType(
    {method.name: method for method in _METHODS}
)


def _callback(name: str, *, mutating: bool) -> CallbackSpec:
    token = name.replace(".", "_")
    return CallbackSpec(
        name=name,
        request_schema_id=f"{_CALLBACK_SCHEMA_ROOT}{token}Request",
        response_schema_id=f"{_CALLBACK_SCHEMA_ROOT}response",
        params_schema_id=f"{_CALLBACK_SCHEMA_ROOT}{token}Params",
        result_schema_id=f"{_CALLBACK_SCHEMA_ROOT}{token}Result",
        mutating=mutating,
    )


_CALLBACKS = (
    _callback("terminal.create", mutating=True),
    _callback("terminal.inventory", mutating=False),
    _callback("terminal.inspect", mutating=False),
    _callback("terminal.deliver", mutating=True),
    _callback("terminal.interrupt", mutating=True),
    _callback("terminal.terminate", mutating=True),
)

CALLBACK_CATALOG: Mapping[str, CallbackSpec] = MappingProxyType(
    {callback.name: callback for callback in _CALLBACKS}
)

__all__ = [
    "CALLBACK_CATALOG",
    "CAPABILITIES",
    "MAX_EXACT_JSON_INTEGER",
    "MAX_FRAME_BYTES",
    "METHOD_CATALOG",
    "PUBLIC_API_MAJOR",
    "PUBLIC_API_MINOR",
    "PUBLIC_LIMITS",
    "CallbackSpec",
    "ConnectionChannel",
    "ConnectionRole",
    "MethodClass",
    "MethodSpec",
]
