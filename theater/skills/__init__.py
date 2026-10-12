"""Declarative, data-only Theater skill packages and plays."""

from theater.skills.loader import is_canonical_name, is_skill_reference, skill_body
from theater.skills.models import (
    BuiltinSkillError,
    Play,
    PlayRules,
    Skill,
    SkillRejection,
    SkillSource,
    SkillValidationError,
    UnknownSkill,
)
from theater.skills.registry import SkillRegistry, discover

__all__ = [
    "BuiltinSkillError",
    "Play",
    "PlayRules",
    "Skill",
    "SkillRegistry",
    "SkillRejection",
    "SkillSource",
    "SkillValidationError",
    "UnknownSkill",
    "discover",
    "is_canonical_name",
    "is_skill_reference",
    "skill_body",
]
