"""Spawn and harness/model discovery RPC handlers."""

from __future__ import annotations

import asyncio
import logging

from theater.daemon.rails import (
    check_budget,
    check_depth,
    check_model_allowed,
    check_reasoning_allowed,
)
from theater.daemon.rpc.params import (
    _prompt_with_response_format,
    _reject_response_format_resume,
    _require,
    _serialized_response_format,
    _validate_worktree_param,
)
from theater.daemon.rpc.router import method
from theater.daemon.spawning.git_cwd import reject_worktree_outside_git
from theater.daemon.spawning.provider_launch import ParticipantLaunchService
from theater.harness import (
    HARNESSES,
    describe,
    native_compatibility_probe,
    native_compatibility_record,
    normalize,
    supports_model,
    supports_reasoning,
)
from theater.harness.channels.health import merge_channel_health
from theater.harness.contracts.channels import ChannelHealth
from theater.harness.contracts.runtime import (
    RuntimeCapability,
    RuntimeCompatibility,
    RuntimeProbeContext,
    RuntimeWiring,
)
from theater.harness.registry.lookup import ProbeOutcome
from theater.models import BadRequest, PublicOperationState, Status, new_id

_WIRING_CHOICES = "auto, native, or legacy"

logger = logging.getLogger(__name__)


def _wiring_param(params: dict) -> RuntimeWiring:
    """Parse the additive ``wiring`` spawn parameter (absent means ``auto``).

    ``legacy`` is the explicit opt-out. Approval has no default and no connection to wiring.
    """
    return wiring_from_value(params.get("wiring"))


def wiring_from_value(raw: object) -> RuntimeWiring:
    """The ``wiring`` choice from a stored or wire value; None means ``auto``."""
    if raw is None:
        return RuntimeWiring.AUTO
    if not isinstance(raw, str):
        raise BadRequest(f"spawn parameter 'wiring' must be a string: {_WIRING_CHOICES}")
    try:
        return RuntimeWiring(raw)
    except ValueError:
        raise BadRequest(
            f"unknown wiring {raw!r}: choose {_WIRING_CHOICES}; 'legacy' is the "
            "explicit opt-out of native runtime wiring"
        ) from None


@method("spawn")
async def _spawn(daemon, params: dict) -> dict:
    provider = params.get("provider")
    if provider is not None and (not isinstance(provider, str) or not provider):
        raise BadRequest("spawn parameter 'provider' must be a non-empty provider id or selector")
    return await _spawn_with_provider(daemon, params, provider)


async def _spawn_with_provider(daemon, params: dict, provider: str | None) -> dict:
    """Adapt the private request to the shared provider-backed launch service."""
    response_format = _serialized_response_format(
        daemon, params, creator_id=params.get("parent_id")
    )
    harness_name = _require(params, "harness")
    _reject_response_format_resume(harness_name, params.get("resume"), response_format)
    rails = daemon.config.rails
    parent_id = params.get("parent_id")
    check_depth(daemon.store, parent_id, cap=rails.depth_cap)
    check_budget(daemon.store, parent_id, limit=rails.budget)
    check_model_allowed(harness_name, params.get("model"), daemon.config.models_for(harness_name))
    check_reasoning_allowed(
        harness_name,
        params.get("reasoning_effort"),
        daemon.config.reasoning_for(harness_name),
    )
    worktree = _validate_worktree_param(params.get("worktree", False))
    if worktree:
        reject_worktree_outside_git(_require(params, "cwd"))
    wiring = _wiring_param(params)
    if params.get("after") is not None or params.get("task_kind") is not None:
        return _defer_until_source(
            daemon, params, response_format, worktree, wiring, provider=provider
        )
    prompt = _prompt_with_response_format(params.get("prompt") or "", response_format)
    request: dict[str, object] = {
        "harness": harness_name,
        "approval": _require(params, "approval"),
        "workspace": {
            "cwd": _require(params, "cwd"),
            "worktree": worktree,
            "base_ref": params.get("base_branch"),
        },
        "initiating_participant_id": params.get("parent_id"),
        "model": params.get("model"),
        "reasoning_effort": params.get("reasoning_effort"),
        "resume": params.get("resume"),
        "name": params.get("name"),
        "description": params.get("description"),
    }
    if prompt:
        request["prompt"] = prompt
    if provider is not None:
        request["provider"] = provider
    key = params.get("idempotency_key")
    if key is None:
        key = f"private-spawn-{new_id()}"
    if not isinstance(key, str) or not key:
        raise BadRequest("spawn parameter 'idempotency_key' must be a non-empty string")
    accepted = await ParticipantLaunchService(daemon).spawn(
        client_id="private-rpc",
        idempotency_key=key,
        params=request,
        launch_prompt=prompt or None,
        launch_wiring=wiring,
        launch_response_format=response_format,
    )
    operation_id = accepted.get("operation_id")
    if not isinstance(operation_id, str):
        raise TypeError("provider-backed spawn acceptance omitted operation_id")
    operation, timed_out = await daemon.operation_service.wait(operation_id)
    if not timed_out and operation.state != "succeeded":
        error = operation.error or {}
        message = str(error.get("message") or "terminal launch did not succeed")
        details = None
        if operation.state == PublicOperationState.UNCERTAIN:
            details = {
                "side_effect": "unknown",
                "participant_id": accepted.get("participant_id"),
                "next": "list_participants to find this participant before spawning a replacement",
            }
        raise BadRequest(f"{message} (operation {operation_id})", details=details)
    participant_id = accepted.get("participant_id")
    if not isinstance(participant_id, str):
        raise TypeError("provider-backed spawn acceptance omitted participant_id")
    participant = daemon.registry.get(participant_id)
    result = participant.to_dict()
    route = daemon.controls.route_for(participant_id, RuntimeCapability.SEND)
    result["addressable"] = participant.status is not Status.DEAD and route.route_available
    result.update(
        {
            # Echo the launch policy so a caller sees which model it actually asked for.
            **{k: params[k] for k in ("model", "reasoning_effort") if params.get(k) is not None},
            "handle": accepted.get("job_handle", participant_id),
            "operation_id": accepted.get("operation_id"),
            "operation_state": operation.state,
        }
    )
    if timed_out:
        result["operation_timed_out"] = True
        result["operation_error"] = operation.error
        result["operation_warning"] = (
            "Terminal launch has not been confirmed. Do not spawn a replacement while this "
            "operation is unresolved; inspect the operation and provider before retrying."
        )
    return result


def _defer_until_source(daemon, params, response_format, worktree, wiring, *, provider) -> dict:
    """Reserve the task now and launch it when its source ends; nothing is created until then."""
    if params.get("idempotency_key") is not None:
        raise BadRequest("spawn parameter 'idempotency_key' is not supported with after")
    return daemon.handoff.admit_spawn(
        {
            **params,
            "approval": _require(params, "approval"),
            "cwd": _require(params, "cwd"),
            "worktree": worktree,
            "wiring": wiring.value,
            "provider": provider,
        },
        task=params.get("prompt") or "",
        response_format=response_format,
    )


@method("harnesses")
async def _harnesses(daemon, params: dict) -> list[dict]:
    """What this daemon can actually spawn.

    Config is read once and never reloaded, so the CLI may see a newer harness set than the
    daemon honours; this prevents offering a spawn the daemon then refuses.
    """
    runtime: dict[str, dict[str, tuple[ChannelHealth, ...]]] = {}
    for participant in daemon.registry.list():
        snapshot = daemon.observer.channel_health_snapshot(participant.id)
        supplemental = (
            *daemon.hook_runtime.health_snapshot(participant.id),
            *daemon.otel_runtime.health_snapshot(participant.id),
        )
        health_by_id: dict[str, ChannelHealth] = {item.channel_id: item for item in snapshot}
        for item in supplemental:
            current = health_by_id.get(item.channel_id)
            health_by_id[item.channel_id] = (
                item if current is None else merge_channel_health(current, item)
            )
        health = tuple(health_by_id.values())
        if health:
            runtime.setdefault(normalize(participant.harness), {})[participant.id] = health
    rows = describe(runtime=runtime)

    async def probe(row: dict) -> ProbeOutcome:
        """One probe attempt; an Exception means the probe failed, not None."""
        harness = HARNESSES.get(row["name"])
        if harness is None or not row["installed"] or row["error"]:
            return None
        callback = native_compatibility_probe(harness)
        if callback is None:
            return None
        try:
            result = await daemon.compatibility_probes.probe(
                row["name"],
                callback,
                RuntimeProbeContext(binary=row["path"]),
                configuration=daemon.config,
            )
        except Exception as error:
            logger.warning(
                "compatibility probe for harness %s failed: %s", row["name"], error, exc_info=error
            )
            return error
        return result if isinstance(result, RuntimeCompatibility) else None

    results = await asyncio.gather(*(probe(row) for row in rows))
    for row, outcome in zip(rows, results, strict=True):
        harness = HARNESSES.get(row["name"])
        if harness is not None:
            row["native_compatibility"] = native_compatibility_record(
                harness,
                installed=bool(row["installed"]),
                result=outcome if isinstance(outcome, RuntimeCompatibility) else None,
                error=outcome if isinstance(outcome, Exception) else None,
            )
    return rows


@method("models")
async def _models(daemon, params: dict) -> list[dict]:
    """The model allowlist this daemon will actually enforce, per harness.

    Like ``harnesses``: the on-disk config may differ from what this daemon holds.
    ``supported`` is adapter capability, ``models`` is user policy — distinct gates.
    """
    rows = []
    for row in describe():
        name = row["name"]
        harness = HARNESSES.get(name)
        rows.append(
            {
                "harness": name,
                "models": daemon.config.models_for(name),
                "supported": harness is not None and supports_model(harness),
                "reasoning": daemon.config.reasoning_for(name),
                "reasoning_supported": harness is not None and supports_reasoning(harness),
                "installed": row["installed"],
                "error": row["error"],
            }
        )
    return rows
