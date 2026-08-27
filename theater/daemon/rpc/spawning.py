"""Spawn and harness/model discovery RPC handlers."""

from __future__ import annotations

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
from theater.daemon.spawning.models import SpawnRequest
from theater.harness import (
    HARNESSES,
    describe,
    normalize,
    supports_model,
    supports_reasoning,
)
from theater.harness.channels.health import merge_channel_health
from theater.harness.contracts.channels import ChannelHealth
from theater.models import JobState


@method("spawn")
async def _spawn(daemon, params: dict) -> dict:
    response_format = _serialized_response_format(params)
    harness_name = _require(params, "harness")
    _reject_response_format_resume(harness_name, params.get("resume"), response_format)
    prompt = _prompt_with_response_format(params.get("prompt") or "", response_format)
    req = SpawnRequest(
        harness=harness_name,
        prompt=prompt,
        cwd=_require(params, "cwd"),
        approval=_require(params, "approval"),
        parent_id=params.get("parent_id"),
        tmux_session=params.get("tmux_session"),
        window_name=params.get("window_name"),
        background=params.get("background", True),
        worktree=_validate_worktree_param(params.get("worktree", False)),
        base_branch=params.get("base_branch"),
        model=params.get("model"),
        reasoning_effort=params.get("reasoning_effort"),
        resume=params.get("resume"),
        response_format=response_format,
    )
    rails = daemon.config.rails
    check_depth(daemon.store, req.parent_id, cap=rails.depth_cap)
    check_budget(daemon.store, req.parent_id, limit=rails.budget)
    # Policy, not capability: the spawner asks the adapter whether it can take a model.
    check_model_allowed(req.harness, req.model, daemon.config.models_for(req.harness))
    check_reasoning_allowed(
        req.harness, req.reasoning_effort, daemon.config.reasoning_for(req.harness)
    )

    # Reserve the participant, worktree, plan, and config files — but not the tmux pane.
    reservation = await daemon.spawner.reserve(req)
    handle = reservation.participant.id
    launched = False
    try:
        daemon.jobs.create(
            handle=handle,
            caller_id=params.get("parent_id") or "cli",
            target_id=reservation.participant.id,
            kind="spawn",
            prompt=req.prompt or "",
            # participant.cwd is the worktree path when worktree=True.
            cwd=reservation.participant.cwd,
            response_format=response_format,
        )
        participant = await daemon.spawner.launch(reservation)
        launched = True
        if not req.prompt:
            # A promptless spawn has nothing to wait for: resolve it now.
            daemon.jobs.finish(handle, state=JobState.DONE, result="")
    except BaseException:
        if not launched:
            await daemon.spawner.cleanup_reservation(reservation.participant)
        job = daemon.jobs.get(handle)
        if job is not None and job.state == JobState.RUNNING:
            daemon.jobs.finish(
                handle,
                state=JobState.CRASHED,
                result="",
                error_code="spawn_failed",
            )
        raise
    result = participant.to_dict()
    result["handle"] = handle
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
    return describe(runtime=runtime)


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
