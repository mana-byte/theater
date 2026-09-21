"""Spawn and harness/model discovery RPC handlers."""

from __future__ import annotations

import asyncio

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
from theater.models import BadRequest, Status, new_id

_WIRING_CHOICES = "auto, native, or legacy"


def _wiring_param(params: dict) -> RuntimeWiring:
    """Parse the additive ``wiring`` spawn parameter at the daemon boundary.

    Absent means ``auto``. ``auto`` and ``native`` prefer compatible native
    wiring and otherwise retain the ordinary launch; ``legacy`` is the
    explicit opt-out. Approval has no default and no connection to wiring.
    """
    raw = params.get("wiring")
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
    response_format = _serialized_response_format(params)
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
    prompt = _prompt_with_response_format(params.get("prompt") or "", response_format)
    wiring = _wiring_param(params)
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
        raise BadRequest(f"{message} (operation {operation_id})")
    participant_id = accepted.get("participant_id")
    if not isinstance(participant_id, str):
        raise TypeError("provider-backed spawn acceptance omitted participant_id")
    participant = daemon.registry.get(participant_id)
    result = participant.to_dict()
    route = daemon.controls.route_for(participant_id, RuntimeCapability.SEND)
    result["addressable"] = participant.status is not Status.DEAD and route.route_available
    result.update(
        {
            "handle": accepted.get("job_handle", participant_id),
            "operation_id": accepted.get("operation_id"),
            "operation_state": operation.state if timed_out else accepted.get("state"),
        }
    )
    if timed_out:
        result["operation_timed_out"] = True
    return result


@method("harnesses")
async def _harnesses(daemon, params: dict) -> list[dict]:
    """What this daemon can actually spawn.

    The registry is importable by anyone, so this looks redundant — but the
    daemon reads its config once at start and never reloads, so a config edit
    leaves the CLI and the régie holding a *newer* harness set than the process
    that has to honour it. Offering a spawn the daemon then refuses is the
    failure this method exists to prevent, and it becomes real the moment the
    set stops being a hardcoded literal.
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

    async def probe(row: dict) -> RuntimeCompatibility | None:
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
        except Exception:
            return None
        return result if isinstance(result, RuntimeCompatibility) else None

    results = await asyncio.gather(*(probe(row) for row in rows))
    for row, result in zip(rows, results, strict=True):
        harness = HARNESSES.get(row["name"])
        if harness is not None:
            row["native_compatibility"] = native_compatibility_record(
                harness,
                installed=bool(row["installed"]),
                result=result,
            )
    return rows


@method("models")
async def _models(daemon, params: dict) -> list[dict]:
    """The model allowlist this daemon will actually enforce, per harness.

    Exists for the same reason as `harnesses`, one level down: the allowlist is
    read out of `daemon.config` at start-up and never reloaded, so after an edit
    the file on disk and the process that refuses the spawn disagree. `theater
    models` reports the file, which is right for a human about to edit it; a
    caller asking "what will be accepted" has to be told what this daemon holds.

    `supported` and `models` are two different gates: `supported` is the
    adapter's capability, `models` is the user's policy. Supported with an
    empty list is one config edit away from working; unsupported cannot take a
    model however the config reads.
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
