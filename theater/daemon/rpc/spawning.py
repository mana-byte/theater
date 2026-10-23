"""Spawn and harness/model discovery RPC handlers."""

from __future__ import annotations

from dataclasses import dataclass

from theater.daemon import catalogs
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
from theater.harness.contracts.runtime import RuntimeCapability, RuntimeWiring
from theater.models import BadRequest, PublicOperationState, Status, new_id


@dataclass(frozen=True, slots=True)
class _PrivateSpawnMapping:
    """Current private spawn mapping to provider and deferred launch requests."""

    params: dict[str, object]
    harness: str
    worktree: bool | str | None
    wiring: RuntimeWiring
    provider: str | None

    def provider_request(self, prompt: str) -> dict[str, object]:
        # Accepted background/window_name/tmux_session inputs do not reach the provider.
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
        # Retain raw omission/null, including ignored presentation, until handoff admission.
        return {
            **self.params,
            "approval": _require(self.params, "approval"),
            "cwd": _require(self.params, "cwd"),
            "worktree": self.worktree,
            "wiring": self.wiring.value,
            "provider": self.provider,
        }


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
    mapping = _PrivateSpawnMapping(params, harness_name, worktree, wiring, provider)
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
    return await catalogs.harnesses(daemon)


@method("models")
async def _models(daemon, params: dict) -> list[dict]:
    return catalogs.models(daemon)
