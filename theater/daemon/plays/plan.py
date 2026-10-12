"""Turn a request's resolved skills into its play facts, against one discovery snapshot."""

from __future__ import annotations

from collections.abc import Sequence

from theater.daemon.plays.models import PlayPlan, PlayRefused
from theater.skills import Skill, SkillRegistry


def plan_for(skills: Sequence[Skill], snapshot: SkillRegistry) -> PlayPlan:
    """Name every attached play member; an attached master skill carries its play's rules."""
    names = tuple(skill.name for skill in skills if skill.play is not None)
    masters = []
    for skill in skills:
        play = snapshot.play(skill.play) if skill.play is not None else None
        if play is not None and play.master_skill == skill.name:
            masters.append(play)
    if len(masters) > 1:
        raise PlayRefused(
            "attach at most one master skill: each starts its own play with the child as master"
        )
    return PlayPlan(names=names, start=masters[0].rules if masters else None)


__all__ = ["plan_for"]
