"""Read the OpenCode 2.x source gate and bump its single exclusive ceiling literal."""

from __future__ import annotations

from pathlib import Path

from ..contract import VerifiedSet, version_key
from . import opencode_bump
from .opencode_bump import RUNTIME_PLAN, rewrite_ceiling

_FLOOR = "OPENCODE_SERVER_V2_MIN_VERSION"
_CEILING = "OPENCODE_SERVER_V2_MAX_VERSION"

__all__ = ["RUNTIME_PLAN", "apply_bump", "read_gate", "verified_set"]


def read_gate(root: Path) -> tuple[str, str]:
    """The 2.x floor and exclusive ceiling as release text (a ceiling may end in .0)."""
    gate = opencode_bump.read_tuples(root, {_FLOOR, _CEILING})
    if gate[_CEILING] <= gate[_FLOOR]:
        raise ValueError("the OpenCode 2.x gate has an invalid exclusive ceiling")
    return opencode_bump.render_release(gate[_FLOOR]), opencode_bump.render_release(gate[_CEILING])


def verified_set(root: Path) -> VerifiedSet:
    """The source gate's inclusive floor and exclusive ceiling."""
    floor, ceiling = read_gate(root)
    return VerifiedSet(
        kind="range",
        floor=floor,
        ceiling=ceiling,
        known_bad=frozenset(),
    )


def apply_bump(root: Path, greens: list[str]) -> list[Path]:
    """Raise only the source ceiling; repeated admitted greens are a no-op."""
    if not greens:
        return []
    keys = [version_key(green) for green in greens]
    if any(key[0] != 2 for key in keys):
        raise ValueError(f"the OpenCode 2.x adapter only admits 2.x releases: {greens}")
    if keys != sorted(keys) or len(set(keys)) != len(keys):
        raise ValueError(f"greens must be unique and ascending: {greens}")
    floor, ceiling = read_gate(root)
    floor_key, ceiling_key = version_key(floor), version_key(ceiling)
    if all(floor_key <= key < ceiling_key for key in keys):
        return []
    if keys[0] < ceiling_key:
        raise ValueError(
            f"green releases must start at or above the current ceiling {ceiling!r}; "
            f"got {greens[0]!r}"
        )
    return rewrite_ceiling(root, _CEILING, greens[-1])
