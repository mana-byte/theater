"""Claude gate parsing and the single-literal bump of its inclusive maximum."""

from __future__ import annotations

import ast
import re
from collections.abc import Sequence
from pathlib import Path

from ..contract import VerifiedSet, load_evidenced, version_key

COMPAT = Path("theater/harness/builtin/plugins/claude/compatibility.py")
_NAMES = ("_HOOK_FLOOR", "_QUALIFIED_MAX")


def _text(version: tuple[int, int, int]) -> str:
    return ".".join(map(str, version))


def gate(root: Path) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """The hook floor and the inclusive qualified maximum, read without importing theater."""
    found: dict[str, tuple[int, int, int]] = {}
    for statement in ast.parse((root / COMPAT).read_text(encoding="utf-8")).body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if isinstance(target, ast.Name) and target.id in _NAMES:
            value = ast.literal_eval(statement.value)
            if not (isinstance(value, tuple) and len(value) == 3):
                raise ValueError(f"invalid Claude gate {target.id}")
            found[target.id] = value
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
        evidenced=load_evidenced(root, "claude"),
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
    anchor = re.search(r"(?m)^_QUALIFIED_MAX = \(\d+, \d+, \d+\)$", source)
    if anchor is None:
        raise ValueError("Claude _QUALIFIED_MAX assignment missing")
    updated = source[: anchor.start()] + f"_QUALIFIED_MAX = {keys[-1]}" + source[anchor.end() :]
    (root / COMPAT).write_text(updated, encoding="utf-8")
    return [COMPAT]
