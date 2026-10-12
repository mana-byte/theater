"""Admit the ``skills`` a spawn or send attaches: resolved once, rendered, and bounded."""

from __future__ import annotations

from theater.constants.skills import SKILL_ATTACH_MAX_BYTES
from theater.daemon.rpc.redirect import _caller_name
from theater.daemon.rpc.skills import _snapshot
from theater.daemon.skill_cards import render_skills
from theater.harness import HARNESSES, normalize
from theater.models import BadRequest, NotFound
from theater.skills import is_canonical_name
from theater.skills.models import UnknownSkill


def _names(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(name, str) for name in raw):
        raise BadRequest(
            "skills must be a list of skill names, exactly as list_skills returns them"
        )
    names = tuple(dict.fromkeys(raw))
    for name in names:
        if not is_canonical_name(name):
            raise BadRequest(
                f"skills entry {name!r} is not a canonical skill name; pass names exactly as "
                "list_skills returns them"
            )
    return names


def attach_skills(daemon, params: dict, *, task: str, sender: str | None) -> str | None:
    """The rendered skill cards for this request, or None when it attaches nothing.

    Resolved from the same snapshot as ``skills.load``, so a disabled built-in is refused here.
    """
    names = _names(params.get("skills"))
    if not names:
        return None
    if not task.strip():
        raise BadRequest(
            "skills needs a prompt: Theater attaches each skill after your task, so state the task"
        )
    snapshot = _snapshot(daemon)
    resolved = []
    for name in names:
        try:
            resolved.append(snapshot.load(name))
        except UnknownSkill:
            raise NotFound(
                f"skill {name!r} is not available: call list_skills and attach an installed, "
                "enabled skill, or leave it out"
            ) from None
    block = render_skills(resolved, attached_by=_caller_name(daemon, sender or "cli"))
    assert block is not None
    size = len(block.encode("utf-8"))
    if size > SKILL_ATTACH_MAX_BYTES:
        raise BadRequest(
            f"the attached skills render to {size} bytes, over the {SKILL_ATTACH_MAX_BYTES}-byte "
            "bound; attach fewer or shorter skills, or ask the agent to call load_skill itself"
        )
    return block


def reject_skills_resume(harness_name: object, resume: object, skills: str | None) -> None:
    if skills is None or not resume or not isinstance(harness_name, str):
        return
    harness = HARNESSES.get(normalize(harness_name))
    if harness is not None and not harness.resume_takes_prompt:
        raise BadRequest(
            f"harness {harness_name!r} cannot resume a session with skills; resume it without "
            "them and use send to deliver the task with its skills"
        )
