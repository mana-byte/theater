"""Immutable values for declarative Theater skills."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class SkillSource(StrEnum):
    """Where a validated skill was discovered."""

    BUILTIN = "builtin"
    MCP_PLUGIN = "mcp_plugin"
    USER = "user"


@dataclass(frozen=True, slots=True)
class Skill:
    """A validated, data-only skill package."""

    name: str
    description: str
    content: str
    source: SkillSource
    source_path: Path
    provider: str | None = None


@dataclass(frozen=True, slots=True)
class SkillRejection:
    """A bounded diagnostic for one rejected skill candidate."""

    source: SkillSource
    source_path: Path
    name: str | None
    error: str
    provider: str | None = None


class SkillValidationError(ValueError):
    """A skill package failed the declarative package contract."""


class BuiltinSkillError(RuntimeError):
    """A Theater-shipped skill failed validation."""


class UnknownSkill(LookupError):
    """A canonical skill name is absent from a snapshot."""
