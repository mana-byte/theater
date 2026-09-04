"""Authenticated, explicit MCP-plugin operation dispatch.

This module deliberately names every operation.  It is the capability
boundary; no plugin request can choose an arbitrary daemon RPC method.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from theater.daemon.persistence.repositories.mcp_plugins import McpPluginCredentialRecord
from theater.daemon.plugins.credentials import credential_id_from_value, verifies
from theater.mcp_plugins.contracts import PluginCapability
from theater.models import (
    BadRequest,
    CapabilityDenied,
    PluginAuthenticationFailed,
)

OperationHandler = Callable[[object, dict[str, Any], str], Awaitable[Any]]

_IDENTITY_FIELDS = frozenset(
    {"actor", "actor_id", "caller", "caller_id", "from", "from_id", "parent", "parent_id"}
)


@dataclass(frozen=True, slots=True)
class PluginOperation:
    """One public sidecar operation and the complete grant it requires."""

    capability: PluginCapability
    handler: OperationHandler
    description: str


def authenticate(daemon, credential: object) -> McpPluginCredentialRecord:
    """Resolve a live sidecar from its opaque credential, or fail closed."""
    credential_id = credential_id_from_value(credential)
    record = (
        daemon.store.get_mcp_plugin_credential(credential_id) if credential_id is not None else None
    )
    if record is None or not verifies(credential, record.credential_verifier):
        raise PluginAuthenticationFailed(
            "plugin credential is invalid, revoked, malformed, or belongs to a dead participant"
        )
    return record


async def dispatch(
    daemon,
    record: McpPluginCredentialRecord,
    operation: object,
    params: object,
) -> Any:
    """Authorize and invoke one fixed operation as the credential's participant."""
    if not isinstance(operation, str) or not operation:
        raise BadRequest("plugin.call requires a non-empty string operation")
    if not isinstance(params, Mapping):
        raise BadRequest("plugin.call parameter 'params' must be a JSON object")
    spec = OPERATIONS.get(operation)
    if spec is None:
        available = ", ".join(sorted(OPERATIONS))
        raise BadRequest(
            f"unknown plugin operation {operation!r}; available operations: {available}"
        )
    if spec.capability not in record.grants:
        raise CapabilityDenied(
            required=spec.capability.value,
            granted=sorted(capability.value for capability in record.grants),
        )
    return await spec.handler(daemon, _without_identity(params), record.participant_id)


def operation_catalog() -> tuple[dict[str, str], ...]:
    """Stable, deterministic metadata for agent-facing CLI/help surfaces."""
    return tuple(
        {
            "operation": name,
            "capability": spec.capability.value,
            "description": spec.description,
        }
        for name, spec in OPERATIONS.items()
    )


def _without_identity(params: Mapping[str, object]) -> dict[str, Any]:
    return {key: value for key, value in params.items() if key not in _IDENTITY_FIELDS}


def _with_actor(
    params: dict[str, Any], actor_id: str, *, field: str = "caller_id"
) -> dict[str, Any]:
    params[field] = actor_id
    return params


async def _participants_list(daemon, params: dict[str, Any], actor_id: str) -> Any:
    del actor_id
    from theater.daemon.rpc.participants import _list

    return await _list(daemon, params)


async def _participants_get(daemon, params: dict[str, Any], actor_id: str) -> Any:
    del actor_id
    from theater.daemon.rpc.participants import _get

    return await _get(daemon, params)


async def _participants_read(daemon, params: dict[str, Any], actor_id: str) -> Any:
    if "id" in params:
        return await _participants_get(daemon, params, actor_id)
    return await _participants_list(daemon, params, actor_id)


async def _participants_tree(daemon, params: dict[str, Any], actor_id: str) -> Any:
    del actor_id, params
    from theater.daemon.rpc.participants import _tree

    return await _tree(daemon, {})


async def _participants_recent_dead(daemon, params: dict[str, Any], actor_id: str) -> Any:
    del actor_id
    from theater.daemon.rpc.participants import _recent_dead

    return await _recent_dead(daemon, params)


async def _participants_update(daemon, params: dict[str, Any], actor_id: str) -> Any:
    from theater.daemon.rpc.participants import _update

    return await _update(daemon, _with_actor(params, actor_id))


async def _catalog(daemon, params: dict[str, Any], actor_id: str) -> dict[str, Any]:
    del params, actor_id
    from theater.daemon.rpc.spawning import _harnesses, _models

    return {"harnesses": await _harnesses(daemon, {}), "models": await _models(daemon, {})}


async def _catalog_harnesses(daemon, params: dict[str, Any], actor_id: str) -> Any:
    del params, actor_id
    from theater.daemon.rpc.spawning import _harnesses

    return await _harnesses(daemon, {})


async def _catalog_models(daemon, params: dict[str, Any], actor_id: str) -> Any:
    del params, actor_id
    from theater.daemon.rpc.spawning import _models

    return await _models(daemon, {})


async def _jobs_get(daemon, params: dict[str, Any], actor_id: str) -> dict[str, Any]:
    del actor_id
    from theater.daemon.rpc.jobs import _jobs_status

    return await _jobs_status(daemon, params)


async def _jobs_await(daemon, params: dict[str, Any], actor_id: str) -> Any:
    from theater.daemon.rpc.jobs import _jobs_await

    return await _jobs_await(daemon, _with_actor(params, actor_id))


async def _transcripts_read(daemon, params: dict[str, Any], actor_id: str) -> Any:
    del actor_id
    from theater.daemon.rpc.transcripts import _read_transcript

    return await _read_transcript(daemon, params)


async def _recall(daemon, params: dict[str, Any], actor_id: str) -> Any:
    del actor_id
    from theater.daemon.rpc.recall import _recall as handler

    return await handler(daemon, params)


async def _recall_read(daemon, params: dict[str, Any], actor_id: str) -> Any:
    del actor_id
    from theater.daemon.rpc.recall import _recall_read as handler

    return await handler(daemon, params)


async def _recall_capability(daemon, params: dict[str, Any], actor_id: str) -> Any:
    if "segment_id" in params:
        return await _recall_read(daemon, params, actor_id)
    return await _recall(daemon, params, actor_id)


async def _skills_list(daemon, params: dict[str, Any], actor_id: str) -> Any:
    del actor_id
    from theater.daemon.rpc.skills import _skills_list

    return await _skills_list(daemon, params)


async def _skills_load(daemon, params: dict[str, Any], actor_id: str) -> Any:
    del actor_id
    from theater.daemon.rpc.skills import _skills_load

    return await _skills_load(daemon, params)


async def _skills_read(daemon, params: dict[str, Any], actor_id: str) -> Any:
    if "name" in params:
        return await _skills_load(daemon, params, actor_id)
    return await _skills_list(daemon, params, actor_id)


async def _trajectory_snapshot(daemon, params: dict[str, Any], actor_id: str) -> Any:
    del actor_id
    from theater.daemon.rpc.trajectory import _trajectory_snapshot

    return await _trajectory_snapshot(daemon, params)


async def _trajectory_follow(daemon, params: dict[str, Any], actor_id: str) -> Any:
    del actor_id
    from theater.daemon.rpc.trajectory import _trajectory_follow

    return await _trajectory_follow(daemon, params)


async def _trajectory_close(daemon, params: dict[str, Any], actor_id: str) -> Any:
    del actor_id
    from theater.daemon.rpc.trajectory import _trajectory_close

    return await _trajectory_close(daemon, params)


async def _trajectory_locate(daemon, params: dict[str, Any], actor_id: str) -> Any:
    del actor_id
    from theater.daemon.rpc.trajectory import _trajectory_locate

    return await _trajectory_locate(daemon, params)


async def _trajectory_search(daemon, params: dict[str, Any], actor_id: str) -> Any:
    del actor_id
    from theater.daemon.rpc.trajectory import _trajectory_search

    return await _trajectory_search(daemon, params)


async def _analytics_stats(daemon, params: dict[str, Any], actor_id: str) -> Any:
    del actor_id
    from theater.daemon.rpc.usage import _stats

    return await _stats(daemon, params)


async def _analytics_usage_totals(daemon, params: dict[str, Any], actor_id: str) -> Any:
    del actor_id
    from theater.daemon.rpc.usage import _usage_totals

    return await _usage_totals(daemon, params)


async def _analytics_usage_summary(daemon, params: dict[str, Any], actor_id: str) -> Any:
    del actor_id
    from theater.daemon.rpc.usage import _usage_summary

    return await _usage_summary(daemon, params)


async def _analytics_usage_by_harness(daemon, params: dict[str, Any], actor_id: str) -> Any:
    del actor_id
    from theater.daemon.rpc.usage import _usage_by_harness

    return await _usage_by_harness(daemon, params)


async def _analytics_bus_tail(daemon, params: dict[str, Any], actor_id: str) -> Any:
    del actor_id
    from theater.daemon.rpc.usage import _bus_tail

    return await _bus_tail(daemon, params)


async def _scratchpad_get(daemon, params: dict[str, Any], actor_id: str) -> Any:
    from theater.daemon.rpc.scratchpad import _scratchpad_get

    return await _scratchpad_get(daemon, _with_actor(params, actor_id))


async def _scratchpad_write(daemon, params: dict[str, Any], actor_id: str) -> Any:
    from theater.daemon.rpc.scratchpad import _scratchpad_write

    return await _scratchpad_write(daemon, _with_actor(params, actor_id))


async def _sessions_spawn(daemon, params: dict[str, Any], actor_id: str) -> Any:
    from theater.daemon.rpc.spawning import _spawn

    return await _spawn(daemon, _with_actor(params, actor_id, field="parent_id"))


async def _sessions_send(daemon, params: dict[str, Any], actor_id: str) -> Any:
    from theater.daemon.rpc.sending import _send

    return await _send(daemon, _with_actor(params, actor_id))


async def _sessions_interrupt(daemon, params: dict[str, Any], actor_id: str) -> Any:
    from theater.daemon.rpc.interruption import _interrupt

    return await _interrupt(daemon, _with_actor(params, actor_id))


async def _sessions_kill(daemon, params: dict[str, Any], actor_id: str) -> Any:
    from theater.daemon.rpc.participants import _kill

    if "id" not in params and "target" in params:
        params["id"] = params.pop("target")
    return await _kill(daemon, _with_actor(params, actor_id))


def _operations() -> Mapping[str, PluginOperation]:
    entries = {
        "participants.list": PluginOperation(
            PluginCapability.PARTICIPANTS_READ, _participants_list, "List participants."
        ),
        "participants.read": PluginOperation(
            PluginCapability.PARTICIPANTS_READ,
            _participants_read,
            "Read one participant when id is present, otherwise list participants.",
        ),
        "participants.get": PluginOperation(
            PluginCapability.PARTICIPANTS_READ, _participants_get, "Read one participant."
        ),
        "participants.tree": PluginOperation(
            PluginCapability.PARTICIPANTS_READ, _participants_tree, "Read the participant tree."
        ),
        "participants.recent_dead": PluginOperation(
            PluginCapability.PARTICIPANTS_READ,
            _participants_recent_dead,
            "List recent retained dead participants.",
        ),
        "participants.update": PluginOperation(
            PluginCapability.PARTICIPANTS_METADATA_WRITE,
            _participants_update,
            "Update participant name or description as the authenticated participant.",
        ),
        "participants.metadata.write": PluginOperation(
            PluginCapability.PARTICIPANTS_METADATA_WRITE,
            _participants_update,
            "Update participant metadata as the authenticated participant.",
        ),
        "catalog.read": PluginOperation(
            PluginCapability.CATALOG_READ, _catalog, "Read the daemon's harness and model catalog."
        ),
        "catalog.harnesses": PluginOperation(
            PluginCapability.CATALOG_READ, _catalog_harnesses, "Read harness catalog entries."
        ),
        "catalog.models": PluginOperation(
            PluginCapability.CATALOG_READ, _catalog_models, "Read model catalog entries."
        ),
        "jobs.get": PluginOperation(
            PluginCapability.JOBS_READ,
            _jobs_get,
            "Read one job by handle.",
        ),
        "jobs.read": PluginOperation(
            PluginCapability.JOBS_READ,
            _jobs_get,
            "Read one job by handle.",
        ),
        "jobs.await": PluginOperation(
            PluginCapability.JOBS_AWAIT, _jobs_await, "Await one or more job handles."
        ),
        "transcripts.read": PluginOperation(
            PluginCapability.TRANSCRIPTS_READ, _transcripts_read, "Read a bounded transcript page."
        ),
        "read_transcript": PluginOperation(
            PluginCapability.TRANSCRIPTS_READ, _transcripts_read, "Read a bounded transcript page."
        ),
        "recall": PluginOperation(
            PluginCapability.RECALL_READ, _recall, "Read file-change recall timelines."
        ),
        "recall.read": PluginOperation(
            PluginCapability.RECALL_READ,
            _recall_capability,
            "Read recall timelines or one segment when segment_id is present.",
        ),
        "recall.segment": PluginOperation(
            PluginCapability.RECALL_READ, _recall_read, "Read one recall timeline segment."
        ),
        "recall_read": PluginOperation(
            PluginCapability.RECALL_READ, _recall_read, "Read one recall timeline segment."
        ),
        "skills.list": PluginOperation(
            PluginCapability.SKILLS_READ, _skills_list, "List daemon-discovered skills."
        ),
        "skills.load": PluginOperation(
            PluginCapability.SKILLS_READ, _skills_load, "Load one named skill."
        ),
        "skills.read": PluginOperation(
            PluginCapability.SKILLS_READ,
            _skills_read,
            "List skills, or load one when name is present.",
        ),
        "trajectory.snapshot": PluginOperation(
            PluginCapability.TRAJECTORY_READ, _trajectory_snapshot, "Read a trajectory snapshot."
        ),
        "trajectory.read": PluginOperation(
            PluginCapability.TRAJECTORY_READ, _trajectory_snapshot, "Read a trajectory snapshot."
        ),
        "trajectory.follow": PluginOperation(
            PluginCapability.TRAJECTORY_READ, _trajectory_follow, "Follow a trajectory stream."
        ),
        "trajectory.close": PluginOperation(
            PluginCapability.TRAJECTORY_READ, _trajectory_close, "Release a trajectory viewer."
        ),
        "trajectory.locate": PluginOperation(
            PluginCapability.TRAJECTORY_READ, _trajectory_locate, "Locate a trajectory record."
        ),
        "trajectory.search": PluginOperation(
            PluginCapability.TRAJECTORY_READ, _trajectory_search, "Search trajectory records."
        ),
        "analytics.stats": PluginOperation(
            PluginCapability.ANALYTICS_READ, _analytics_stats, "Read turn statistics."
        ),
        "analytics.read": PluginOperation(
            PluginCapability.ANALYTICS_READ, _analytics_stats, "Read turn statistics."
        ),
        "analytics.usage_totals": PluginOperation(
            PluginCapability.ANALYTICS_READ, _analytics_usage_totals, "Read aggregate usage totals."
        ),
        "analytics.usage_summary": PluginOperation(
            PluginCapability.ANALYTICS_READ, _analytics_usage_summary, "Read usage summary windows."
        ),
        "analytics.usage_by_harness": PluginOperation(
            PluginCapability.ANALYTICS_READ,
            _analytics_usage_by_harness,
            "Read usage by harness.",
        ),
        "analytics.bus_tail": PluginOperation(
            PluginCapability.ANALYTICS_READ, _analytics_bus_tail, "Read normalized bus events."
        ),
        "scratchpad.get": PluginOperation(
            PluginCapability.SCRATCHPAD_READ, _scratchpad_get, "Read the actor tree's scratchpad."
        ),
        "scratchpad.read": PluginOperation(
            PluginCapability.SCRATCHPAD_READ, _scratchpad_get, "Read the actor tree's scratchpad."
        ),
        "scratchpad.write": PluginOperation(
            PluginCapability.SCRATCHPAD_WRITE,
            _scratchpad_write,
            "Write the actor tree's scratchpad.",
        ),
        "sessions.spawn": PluginOperation(
            PluginCapability.SESSIONS_SPAWN,
            _sessions_spawn,
            "Spawn a child with the authenticated participant as parent.",
        ),
        "sessions.send": PluginOperation(
            PluginCapability.SESSIONS_SEND,
            _sessions_send,
            "Send a prompt using ordinary busy and human-presence safeguards.",
        ),
        "sessions.interrupt": PluginOperation(
            PluginCapability.SESSIONS_INTERRUPT,
            _sessions_interrupt,
            "Interrupt an eligible child using ordinary lifecycle safeguards.",
        ),
        "sessions.kill": PluginOperation(
            PluginCapability.SESSIONS_KILL,
            _sessions_kill,
            "Kill an eligible child using ordinary lifecycle safeguards.",
        ),
    }
    return MappingProxyType(entries)


OPERATIONS = _operations()

__all__ = ["OPERATIONS", "PluginOperation", "authenticate", "dispatch", "operation_catalog"]
