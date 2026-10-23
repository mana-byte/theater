"""Advance Vibe's exclusive ceiling to the patch after the last green."""

from __future__ import annotations

import ast
from collections.abc import Sequence
from pathlib import Path

from ..contract import version_key
from ..gate_literals import assignments as literal_assignments
from ..gate_literals import replace_tuple_line

COMPAT = Path("theater/harness/builtin/plugins/vibe/compatibility.py")


def gate(root: Path) -> tuple[str, str]:
    source = (root / COMPAT).read_text()
    found: dict[str, tuple[int, int, int]] = {}
    for assignment in literal_assignments(source, {"_FLOOR", "_CEILING"}):
        if not isinstance(assignment.statement, ast.Assign):
            continue
        value = assignment.literal()
        if not isinstance(value, tuple) or len(value) != 3:
            raise ValueError(f"invalid {assignment.name} Vibe gate")
        found[assignment.name] = value
    missing = {"_FLOOR", "_CEILING"} - found.keys()
    if missing:
        raise ValueError(f"Vibe gate assignment missing: {', '.join(sorted(missing))}")
    return (
        ".".join(map(str, found["_FLOOR"])),
        ".".join(map(str, found["_CEILING"])),
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
    compat_updated = replace_tuple_line(
        compat_source, "_CEILING", ceiling_key, "Vibe ceiling assignment missing"
    )
    (root / COMPAT).write_text(compat_updated)
    return [COMPAT]
