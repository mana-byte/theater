"""Claude gate parsing and the single-literal bump of its inclusive maximum."""

from __future__ import annotations

import ast
from collections.abc import Sequence
from pathlib import Path

from ..contract import VerifiedSet, version_key
from ..gate_literals import assignments, replace_tuple_line

COMPAT = Path("theater/harness/builtin/plugins/claude/compatibility.py")
_NAMES = ("_HOOK_FLOOR", "_QUALIFIED_MAX")


def _text(version: tuple[int, int, int]) -> str:
    return ".".join(map(str, version))


def gate(root: Path) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """The hook floor and the inclusive qualified maximum, read without importing theater."""
    found: dict[str, tuple[int, int, int]] = {}
    for assignment in assignments((root / COMPAT).read_text(encoding="utf-8"), _NAMES):
        if not isinstance(assignment.statement, ast.Assign):
            continue
        value = assignment.literal()
        if not (isinstance(value, tuple) and len(value) == 3):
            raise ValueError(f"invalid Claude gate {assignment.name}")
        found[assignment.name] = value
    if missing := set(_NAMES) - found.keys():
        raise ValueError(f"Claude gate assignment missing: {', '.join(sorted(missing))}")
    return found["_HOOK_FLOOR"], found["_QUALIFIED_MAX"]


def verified(root: Path) -> VerifiedSet:
    floor, maximum = gate(root)
    major, minor, patch = maximum
    return VerifiedSet(
        "range",
        floor=_text(floor),
        ceiling=_text((major, minor, patch + 1)),
    )


def bump(root: Path, greens: Sequence[str]) -> list[Path]:
    if not greens:
        return []
    keys = [version_key(version) for version in greens]
    if keys != sorted(set(keys)):
        raise ValueError("Claude greens must be unique and ascending")
    floor, maximum = gate(root)
    if keys[0] < floor:
        raise ValueError("Claude green is below the floor")
    if keys[-1] <= maximum:
        return []
    source = (root / COMPAT).read_text(encoding="utf-8")
    updated = replace_tuple_line(
        source, "_QUALIFIED_MAX", keys[-1], "Claude _QUALIFIED_MAX assignment missing"
    )
    (root / COMPAT).write_text(updated, encoding="utf-8")
    return [COMPAT]
