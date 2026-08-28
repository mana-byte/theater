"""Declarative, data-only Theater skill packages."""

from theater.skills.loader import is_canonical_name
from theater.skills.models import (
    BuiltinSkillError,
    Skill,
    SkillRejection,
    SkillSource,
    SkillValidationError,
    UnknownSkill,
)
from theater.skills.registry import SkillRegistry, discover

__all__ = [
    "BuiltinSkillError",
    "Skill",
    "SkillRegistry",
    "SkillRejection",
    "SkillSource",
    "SkillValidationError",
    "UnknownSkill",
    "discover",
    "is_canonical_name",
]
