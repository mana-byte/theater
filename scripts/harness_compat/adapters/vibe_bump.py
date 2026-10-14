"""Advance Vibe's exclusive ceiling to the patch after the last green."""

from __future__ import annotations

import ast
import re
from collections.abc import Sequence
from pathlib import Path

from ..contract import version_key

COMPAT = Path("theater/harness/builtin/plugins/vibe/compatibility.py")


def gate(root: Path) -> tuple[str, str]:
    source = (root / COMPAT).read_text()
    assignments: dict[str, tuple[int, int, int]] = {}
    for statement in ast.parse(source).body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            if isinstance(target, ast.Name) and target.id in {"_FLOOR", "_CEILING"}:
                value = ast.literal_eval(statement.value)
                if not isinstance(value, tuple) or len(value) != 3:
                    raise ValueError(f"invalid {target.id} Vibe gate")
                assignments[target.id] = value
    missing = {"_FLOOR", "_CEILING"} - assignments.keys()
    if missing:
        raise ValueError(f"Vibe gate assignment missing: {', '.join(sorted(missing))}")
    return (
        ".".join(map(str, assignments["_FLOOR"])),
        ".".join(map(str, assignments["_CEILING"])),
    )


def bump(root: Path, greens: Sequence[str]) -> list[Path]:
    if not greens:
        return []
    keys = [version_key(version) for version in greens]
    if keys != sorted(set(keys)):
        raise ValueError("Vibe greens must be unique and ascending")
    floor, old_ceiling = gate(root)
    if keys[0] < version_key(floor):
        raise ValueError("Vibe green is below the floor")
    major, minor, patch = keys[-1]
    ceiling_key = (major, minor, patch + 1)
    if ceiling_key <= version_key(old_ceiling):
        return []

    compat_source = (root / COMPAT).read_text()
    before = re.search(r"(?m)^_CEILING = \(\d+, \d+, \d+\)$", compat_source)
    if before is None:
        raise ValueError("Vibe ceiling assignment missing")
    compat_updated = (
        compat_source[: before.start()]
        + f"_CEILING = {ceiling_key}"
        + compat_source[before.end() :]
    )
    (root / COMPAT).write_text(compat_updated)
    return [COMPAT]
