"""Dependency-free, fail-closed support policy shared by plugins and CI reporting."""

# Invalid policy input consistently raises ValueError for JSON and direct callers.
# ruff: noqa: TRY004

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def version_key(version: str) -> tuple[int, int, int]:
    """Validate a canonical stable MAJOR.MINOR.PATCH release."""
    match = _VERSION.fullmatch(version) if isinstance(version, str) else None
    if match is None:
        raise ValueError(f"expected a canonical stable MAJOR.MINOR.PATCH version, got {version!r}")
    return int(match[1]), int(match[2]), int(match[3])


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate compatibility policy key: {key!r}")
        value[key] = item
    return value


@dataclass(frozen=True, slots=True)
class VersionPolicy:
    minimum: str
    maximum: str
    excluded: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        minimum, maximum = version_key(self.minimum), version_key(self.maximum)
        if minimum > maximum:
            raise ValueError("compatibility policy minimum must not exceed maximum")
        if not isinstance(self.excluded, tuple):
            raise ValueError("compatibility policy excluded must be a tuple of versions")
        seen: set[str] = set()
        for version in self.excluded:
            key = version_key(version)
            if version in seen:
                raise ValueError(f"duplicate excluded version: {version!r}")
            if key < minimum:
                raise ValueError(f"excluded version {version!r} is below the policy minimum")
            seen.add(version)

    def admits(self, version: str) -> bool:
        try:
            key = version_key(version)
        except ValueError:
            return False
        return (
            version_key(self.minimum) <= key <= version_key(self.maximum)
            and version not in self.excluded
        )

    @property
    def range_text(self) -> str:
        text = f">={self.minimum},<={self.maximum}"
        if self.excluded:
            text += " excluding " + ", ".join(self.excluded)
        return text

    def to_json(self) -> dict[str, object]:
        return {"min": self.minimum, "max": self.maximum, "excluded": list(self.excluded)}

    @classmethod
    def from_json(cls, value: object) -> VersionPolicy:
        if not isinstance(value, dict) or value.keys() != {"min", "max", "excluded"}:
            raise ValueError("compatibility policy must contain exactly min, max, excluded")
        if not isinstance(value["min"], str) or not isinstance(value["max"], str):
            raise ValueError("compatibility policy min and max must be version strings")
        if not isinstance(value["excluded"], list):
            raise ValueError("compatibility policy excluded must be a list of version strings")
        return cls(value["min"], value["max"], tuple(value["excluded"]))

    @classmethod
    def from_text(cls, text: str) -> VersionPolicy:
        return cls.from_json(json.loads(text, object_pairs_hook=_unique_object))

    @classmethod
    def load(cls, path: Path) -> VersionPolicy:
        return cls.from_text(path.read_text(encoding="utf-8"))
