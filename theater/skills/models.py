"""Immutable values for declarative Theater skills and plays."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

from theater.constants.skills import PLAY_SEPARATOR


class SkillSource(StrEnum):
    """Where a validated skill was discovered."""

    BUILTIN = "builtin"
    MCP_PLUGIN = "mcp_plugin"
    USER = "user"


@dataclass(frozen=True, slots=True)
class Skill:
    """A validated, data-only skill package; a play member's name is ``<play>/<member>``."""

    name: str
    description: str
    content: str
    source: SkillSource
    source_path: Path
    provider: str | None = None
    play: str | None = None


def _frozen(mapping: Mapping[str, tuple[str, ...]]) -> Mapping[str, tuple[str, ...]]:
    return MappingProxyType({key: tuple(mapping[key]) for key in sorted(mapping)})


@dataclass(frozen=True, slots=True, eq=False)
class PlayRules:
    """A play's structure: its master position, who may spawn whom, and the task roles.

    Pinned when a play starts, so editing PLAY.toml never changes a live play.
    """

    play: str
    master: str
    #: position -> the positions it may give a child
    positions: Mapping[str, tuple[str, ...]]
    #: task role -> the positions it fits
    roles: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "positions", _frozen(self.positions))
        object.__setattr__(self, "roles", _frozen(self.roles))

    def qualified(self, member: str) -> str:
        return f"{self.play}{PLAY_SEPARATOR}{member}"

    def member(self, name: str) -> str | None:
        """The member part of a qualified name of this play, else None."""
        prefix = f"{self.play}{PLAY_SEPARATOR}"
        return name[len(prefix) :] if name.startswith(prefix) else None

    def children(self, position: str) -> tuple[str, ...]:
        return self.positions.get(position, ())

    def roles_for(self, position: str) -> tuple[str, ...]:
        return tuple(role for role, fits in self.roles.items() if position in fits)

    def to_json(self) -> str:
        return json.dumps(
            {
                "play": self.play,
                "master": self.master,
                "positions": {key: sorted(value) for key, value in self.positions.items()},
                "roles": {key: sorted(value) for key, value in self.roles.items()},
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_json(cls, text: str) -> PlayRules:
        data = json.loads(text)
        return cls(
            play=data["play"],
            master=data["master"],
            positions={key: tuple(value) for key, value in data["positions"].items()},
            roles={key: tuple(value) for key, value in data["roles"].items()},
        )


@dataclass(frozen=True, slots=True, eq=False)
class Play:
    """A validated play package: PLAY.toml plus one member skill per position and role."""

    name: str
    description: str
    rules: PlayRules
    source: SkillSource
    source_path: Path

    @property
    def master_skill(self) -> str:
        return self.rules.qualified(self.rules.master)


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
