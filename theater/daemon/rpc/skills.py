"""Daemon-owned discovery and loading of declarative skills and plays."""

from __future__ import annotations

from theater.daemon.rpc.params import _string_param
from theater.daemon.rpc.router import method
from theater.mcp_plugins import registry as mcp_registry
from theater.models import BadRequest, NotFound
from theater.skills import is_skill_reference, registry
from theater.skills.models import Play, UnknownSkill


def _skill_metadata(skill) -> dict[str, str]:
    metadata = {
        "name": skill.name,
        "description": skill.description,
        "source": str(skill.source),
    }
    if skill.provider is not None:
        metadata["plugin"] = skill.provider
    if skill.play is not None:
        metadata["play"] = skill.play
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


def _play_view(play: Play, snapshot) -> dict[str, object]:
    """One play with each member described once: positions with the positions they may give."""
    rules = play.rules

    def described(member: str) -> dict[str, object]:
        return {"name": rules.qualified(member), **_member_text(snapshot, rules.qualified(member))}

    return {
        "name": play.name,
        "description": play.description,
        "source": str(play.source),
        "master": play.master_skill,
        "positions": [
            {
                **described(position),
                "spawns": [rules.qualified(child) for child in rules.children(position)],
            }
            for position in _ordered_positions(play)
        ],
        "roles": [
            {**described(role), "fits": [rules.qualified(fit) for fit in rules.roles[role]]}
            for role in rules.roles
        ],
    }


def _member_text(snapshot, name: str) -> dict[str, str]:
    try:
        return {"description": snapshot.load(name).description}
    except UnknownSkill:
        return {}


def _ordered_positions(play: Play) -> list[str]:
    """The master first, then each position after the one that spawns it."""
    rules = play.rules
    ordered = [rules.master]
    for position in ordered:
        ordered.extend(child for child in rules.children(position) if child not in ordered)
    return ordered


@method("skills.list")
async def _skills_list(daemon, params: dict) -> dict:
    """Flat by default for CLI and frontend; ``grouped`` puts play members under their play."""
    snapshot = _snapshot(daemon)
    rejections = [_rejection_diagnostic(rejection) for rejection in snapshot.rejections]
    play_name = params.get("play")
    if play_name is not None:
        if not isinstance(play_name, str) or snapshot.play(play_name) is None:
            known = ", ".join(play.name for play in snapshot.plays) or "none"
            raise NotFound(f"no play named {play_name!r}; installed plays: {known}")
        return {"plays": [_play_view(snapshot.play(play_name), snapshot)]}
    if params.get("grouped") is True:
        return {
            "skills": [_skill_metadata(skill) for skill in snapshot.skills if skill.play is None],
            "plays": [_play_view(play, snapshot) for play in snapshot.plays],
            "rejections": rejections,
        }
    return {
        "skills": [_skill_metadata(skill) for skill in snapshot.skills],
        "rejections": rejections,
    }


@method("skills.load")
async def _skills_load(daemon, params: dict) -> dict:
    """Read one skill; an identified participant loading a play's master skill starts it."""
    try:
        name = _string_param(params, "name", method_name="skills.load")
    except BadRequest as exc:
        raise BadRequest(
            "skills.load requires a non-empty string parameter 'name'; "
            "call skills.list and pass one returned name"
        ) from exc
    if not is_skill_reference(name):
        raise BadRequest(
            "skills.load parameter 'name' must be a canonical skill name; "
            "call skills.list and pass one returned name"
        )
    snapshot = _snapshot(daemon)
    try:
        skill = snapshot.load(name)
    except UnknownSkill as exc:
        raise NotFound(
            f"skill {name!r} is not available; call skills.list to select an installed skill"
        ) from exc
    loaded: dict[str, object] = {**_skill_metadata(skill), "content": skill.content}
    # Only an agent's own load names a caller; frontend and CLI reads never start a play.
    caller_id = params.get("caller_id")
    play = snapshot.play(skill.play) if skill.play is not None else None
    if isinstance(caller_id, str) and caller_id and play and play.master_skill == skill.name:
        loaded["play_start"] = daemon.plays.start_by_load(caller_id, play)
    return loaded
