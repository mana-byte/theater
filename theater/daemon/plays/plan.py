"""Turn a request's resolved skills into its play facts, against one discovery snapshot."""

from __future__ import annotations

from collections.abc import Sequence

from theater.daemon.plays.models import PlayPlan, PlayRefused
from theater.skills import Skill, SkillRegistry


def plan_for(skills: Sequence[Skill], snapshot: SkillRegistry) -> PlayPlan:
    """Name every attached play member; an attached master skill carries its play's rules."""
    names = tuple(skill.name for skill in skills if skill.play is not None)
    plays = [snapshot.play(name) for name in dict.fromkeys(s.play for s in skills if s.play)]
    starts = [play for play in plays if play is not None and play.master_skill in names]
    if len(starts) > 1:
        raise PlayRefused(
            "attach at most one master skill: each starts its own play with the child as master"
        )
    return PlayPlan(
        names=names,
        start=starts[0].rules if starts else None,
        masters=tuple(play.master_skill for play in plays if play is not None),
    )


__all__ = ["plan_for"]
