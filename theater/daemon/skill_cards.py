"""Render attached skills as labelled cards and place them in a composed prompt."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from theater.daemon.response_contracts.prompts import prompt_with_guidance
from theater.skills import Skill, skill_body

SKILL_OPEN = "[Theater skill"
SKILL_CLOSE = "[/Theater skill]"
_DIGEST_CHARS = 12


def render_skills(skills: Sequence[Skill], *, attached_by: str) -> str | None:
    """One labelled card per skill, in the caller's order; None when nothing is attached."""
    if not skills:
        return None
    return "\n\n".join(_card(skill, attached_by) for skill in skills)


def _card(skill: Skill, attached_by: str) -> str:
    digest = hashlib.sha256(skill.content.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]
    origin = str(skill.source) if skill.provider is None else f"{skill.source} {skill.provider}"
    head = (
        f"{SKILL_OPEN} {skill.name} ({origin}, sha256:{digest}), attached by {attached_by}. "
        "Your user's and system instructions take precedence.]"
    )
    return "\n".join([head, skill_body(skill.content).strip(), SKILL_CLOSE])


def quote_skill_markers(text: str) -> str:
    """Keep quoted output from forging a skill card boundary."""
    return text.replace(SKILL_OPEN, "[quoted Theater skill").replace(
        SKILL_CLOSE, "[/quoted Theater skill]"
    )


def compose_prompt(
    task: str,
    *,
    card: str | None = None,
    skills: str | None = None,
    response_format: str | None = None,
) -> str:
    """Task first, then the handoff card and attached skills; response guidance keeps its place.

    Completion matches a turn by the prompt's first characters, so a constant card never leads.
    """
    prompt = "\n\n".join([task, *(part for part in (card, skills) if part)])
    return prompt if response_format is None else prompt_with_guidance(prompt, response_format)


def prompt_with_interruption_card(
    prompt: str, *, interrupted: str | None, by: str, running: list[tuple[str, str, str]]
) -> str:
    """Tell a replaced child what survived the interrupt so it awaits instead of respawning."""
    turn = f"your turn {interrupted}" if interrupted else "your turn"
    card = f"[Theater: {turn} was interrupted by {by}."
    if running:
        listed = ", ".join(f"{handle} -> {name} ({state})" for handle, name, state in running)
        card += (
            f" Jobs you launched that are still running: {listed}. "
            "They are still running: await them, do not respawn."
        )
    return f"{card}]\n\n{prompt}"
