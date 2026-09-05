"""Daemon-owned discovery and loading of declarative skills."""

from __future__ import annotations

from theater.daemon.rpc.params import _string_param
from theater.daemon.rpc.router import method
from theater.mcp_plugins import registry as mcp_registry
from theater.models import BadRequest, NotFound
from theater.skills import is_canonical_name, registry
from theater.skills.models import UnknownSkill


def _skill_metadata(skill) -> dict[str, str]:
    metadata = {
        "name": skill.name,
        "description": skill.description,
        "source": str(skill.source),
    }
    if skill.provider is not None:
        metadata["plugin"] = skill.provider
    return metadata


def _rejection_diagnostic(rejection) -> dict[str, str | None]:
    diagnostic = {
        "source": str(rejection.source),
        "path": str(rejection.source_path),
        "name": rejection.name,
        "error": rejection.error,
    }
    if rejection.provider is not None:
        diagnostic["plugin"] = rejection.provider
    return diagnostic


def _snapshot(daemon):
    return registry.discover(
        plugin_skills=mcp_registry.catalog().registered_skills,
        disabled=daemon.config.skills.disabled,
    )


@method("skills.list")
async def _skills_list(daemon, params: dict) -> dict:
    snapshot = _snapshot(daemon)
    return {
        "skills": [_skill_metadata(skill) for skill in snapshot.skills],
        "rejections": [_rejection_diagnostic(rejection) for rejection in snapshot.rejections],
    }


@method("skills.load")
async def _skills_load(daemon, params: dict) -> dict:
    try:
        name = _string_param(params, "name", method_name="skills.load")
    except BadRequest as exc:
        raise BadRequest(
            "skills.load requires a non-empty string parameter 'name'; "
            "call skills.list and pass one returned name"
        ) from exc
    if not is_canonical_name(name):
        raise BadRequest(
            "skills.load parameter 'name' must be a canonical skill name; "
            "call skills.list and pass one returned name"
        )
    try:
        skill = _snapshot(daemon).load(name)
    except UnknownSkill as exc:
        raise NotFound(
            f"skill {name!r} is not available; call skills.list to select an installed skill"
        ) from exc
    return {**_skill_metadata(skill), "content": skill.content}
