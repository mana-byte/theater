"""Spawn and harness/model discovery RPC handlers."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TypedDict

from theater.daemon.rails import (
    check_budget,
    check_depth,
    check_model_allowed,
    check_reasoning_allowed,
)
from theater.daemon.rpc.params import (
    _reject_response_format_resume,
    _require,
    _serialized_response_format,
    _validate_worktree_param,
)
from theater.daemon.rpc.router import method
from theater.daemon.rpc.skill_attachments import (
    Attachment,
    attach_skills,
    reject_skills_resume,
)
from theater.daemon.skill_cards import compose_prompt
from theater.daemon.spawning.git_cwd import reject_worktree_outside_git
from theater.daemon.spawning.models import wiring_from_value
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

logger = logging.getLogger(__name__)


class _PresentationCompatibility(TypedDict, total=False):
    background: object
    window_name: object
    tmux_session: object


@dataclass(frozen=True, slots=True)
class _PrivateSpawnMapping:
    """Current private spawn mapping, including accepted but unforwarded presentation input."""

    params: dict[str, object]
    harness: str
    worktree: bool | str | None
    wiring: RuntimeWiring
    provider: str | None
    compatibility: _PresentationCompatibility

    def provider_request(self, prompt: str) -> dict[str, object]:
        request: dict[str, object] = {
            "harness": self.harness,
            "approval": _require(self.params, "approval"),
            "workspace": {
                "cwd": _require(self.params, "cwd"),
                "worktree": self.worktree,
                "base_ref": self.params.get("base_branch"),
            },
            "initiating_participant_id": self.params.get("parent_id"),
            "model": self.params.get("model"),
            "reasoning_effort": self.params.get("reasoning_effort"),
            "resume": self.params.get("resume"),
            "name": self.params.get("name"),
            "description": self.params.get("description"),
        }
        if prompt:
            request["prompt"] = prompt
        if self.provider is not None:
            request["provider"] = self.provider
        return request

    def deferred_payload(self) -> dict[str, object]:
        # Handoff admission stores its own whitelist; retain raw omission/null until then.
        return {
            **self.params,
            "approval": _require(self.params, "approval"),
            "cwd": _require(self.params, "cwd"),
            "worktree": self.worktree,
            "wiring": self.wiring.value,
            "provider": self.provider,
        }


def _private_spawn_mapping(
    params: dict[str, object],
    harness: str,
    worktree: bool | str | None,
    wiring: RuntimeWiring,
    provider: str | None,
) -> _PrivateSpawnMapping:
    # These published CLI/plugin inputs are accepted; current private launch ignores them.
    compatibility: _PresentationCompatibility = {}
    if "background" in params:
        compatibility["background"] = params["background"]
    if "window_name" in params:
        compatibility["window_name"] = params["window_name"]
    if "tmux_session" in params:
        compatibility["tmux_session"] = params["tmux_session"]
    return _PrivateSpawnMapping(params, harness, worktree, wiring, provider, compatibility)


def _wiring_param(params: dict) -> RuntimeWiring:
    """Parse the additive ``wiring`` spawn parameter (absent means ``auto``).

    ``legacy`` is the explicit opt-out. Approval has no default and no connection to wiring.
    """
    return wiring_from_value(params.get("wiring"))


@method("spawn")
async def _spawn(daemon, params: dict) -> dict:
    provider = params.get("provider")
    if provider is not None and (not isinstance(provider, str) or not provider):
        raise BadRequest("spawn parameter 'provider' must be a non-empty provider id or selector")
    return await _spawn_with_provider(daemon, params, provider)


def _resume_safe_skills(
    daemon, params: dict, harness_name: str, response_format: str | None
) -> Attachment:
    """The rendered skills; a resume that would drop them or the guidance is refused."""
    resume = params.get("resume")
    _reject_response_format_resume(harness_name, resume, response_format)
    attachment = attach_skills(
        daemon, params, task=params.get("prompt") or "", sender=params.get("parent_id")
    )
    reject_skills_resume(harness_name, resume, attachment.block)
    return attachment


async def _spawn_with_provider(daemon, params: dict, provider: str | None) -> dict:
    """Adapt the private request to the shared provider-backed launch service."""
    response_format = _serialized_response_format(
        daemon, params, creator_id=params.get("parent_id")
    )
    harness_name = _require(params, "harness")
    attachment = _resume_safe_skills(daemon, params, harness_name, response_format)
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
    mapping = _private_spawn_mapping(params, harness_name, worktree, wiring, provider)
    if params.get("after") is not None or params.get("task_kind") is not None:
        return _defer_until_source(
            daemon,
            mapping,
            response_format,
            attachment=attachment,
        )
    prompt = compose_prompt(
        params.get("prompt") or "", skills=attachment.block, response_format=response_format
    )
    request = mapping.provider_request(prompt)
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
        launch_wiring=mapping.wiring,
        launch_response_format=response_format,
        launch_play=attachment.plan,
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
            "Terminal launch has not been confirmed. await_sessions([handle]) settles it "
            "(the job ends crashed if the launch fails) and list_participants(ids=[id]) "
            "shows the child once addressable. Do not spawn a replacement meanwhile."
        )
    return result


def _defer_until_source(
    daemon, mapping: _PrivateSpawnMapping, response_format, *, attachment: Attachment
) -> dict:
    """Reserve the task now and launch it when its source ends; nothing is created until then."""
    params = mapping.params
    if params.get("idempotency_key") is not None:
        raise BadRequest("spawn parameter 'idempotency_key' is not supported with after")
    return daemon.handoff.admit_spawn(
        mapping.deferred_payload(),
        task=params.get("prompt") or "",
        response_format=response_format,
        skills=attachment.block,
        play=attachment.plan,
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
