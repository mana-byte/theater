"""Admit a green Vibe minor line up to its next exclusive minor boundary.

Untested in-range patches stay admitted and are covered by regression watch.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Sequence
from pathlib import Path

from ..contract import version_key

COMPAT = Path("theater/harness/builtin/plugins/vibe/compatibility.py")
COMPAT_TEST = Path("tests/test_vibe_compatibility.py")
DOC = Path("docs/native-interaction/vibe.md")


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
    return tuple(".".join(map(str, assignments[name])) for name in ("_FLOOR", "_CEILING"))  # type: ignore[return-value]


def bump(root: Path, greens: Sequence[str]) -> list[Path]:
    if not greens:
        return []
    keys = [version_key(version) for version in greens]
    if keys != sorted(set(keys)):
        raise ValueError("Vibe greens must be unique and ascending")
    floor, old_ceiling = gate(root)
    if keys[0] < version_key(floor):
        raise ValueError("Vibe green is below the floor")
    major, minor, _patch = keys[-1]
    new_ceiling = f"{major}.{minor + 1}.0"
    if version_key(new_ceiling) <= version_key(old_ceiling):
        return []
    changed: list[Path] = []
    source = (root / COMPAT).read_text()
    before = re.search(r"(?m)^_CEILING = \(\d+, \d+, \d+\)$", source)
    if before is None:
        raise ValueError("Vibe ceiling assignment missing")
    updated = (
        source[: before.start()] + f"_CEILING = ({major}, {minor + 1}, 0)" + source[before.end() :]
    )
    updated = updated.replace(
        "#: The newest Vibe release line whose unified session store this reader\n"
        "#: understands: 2.26.x writes store_format_minor 8 (vibe 376f6a33). The probe is\n"
        "#: display-only, so launch planning enforces the same ceiling (launch.plan_launch);\n"
        "#: bump the ceiling in the same commit as unified_store_types.STORE_FORMAT_MINOR —\n"
        "#: the qualified range once outran the reader, and finished jobs waited forever on\n"
        "#: stores no reader could open.",
        "#: Exclusive minor boundary. New patches remain admitted and are watched for\n"
        "#: reader regressions; a new minor line needs a green qualification. Launch\n"
        "#: planning enforces this ceiling because the probe is display-only.",
    )
    (root / COMPAT).write_text(updated)
    changed.append(COMPAT)

    source = (root / COMPAT_TEST).read_text()
    source = source.replace(f">={floor},<{old_ceiling}", f">={floor},<{new_ceiling}")
    source = source.replace(
        f'("vibe {old_ceiling}", False, "{old_ceiling}",',
        f'("vibe {new_ceiling}", False, "{new_ceiling}",',
    )
    source = source.replace(f"== {version_key(old_ceiling)}", f"== {version_key(new_ceiling)}")
    source = re.sub(
        r"(?:2\.26\.x writes store_format_minor 8 \(vibe 376f6a33\); the ceiling is the next\n"
        r"    release line, and the reader understands exactly that minor\.|"
        r"Verified releases through \d+\.\d+\.\d+ write a store the reader understands;\n"
        r"    the next patch is held for verification\.)",
        "Verified Vibe releases write a store the reader understands;\n"
        "    the next minor line is held for verification.",
        source,
    )
    (root / COMPAT_TEST).write_text(source)
    changed.append(COMPAT_TEST)

    doc = root / DOC
    if doc.exists():
        source = doc.read_text()
        old = f"Vibe compatibility range: >={floor},<{old_ceiling}."
        new = f"Vibe compatibility range: >={floor},<{new_ceiling}."
        if old in source:
            updated = source.replace(old, new)
        else:
            marker = "## Current baseline\n"
            if marker not in source:
                raise ValueError("Vibe docs baseline section missing")
            updated = source.replace(marker, f"{marker}\n{new}\n", 1)
        if updated != source:
            doc.write_text(updated)
            changed.append(DOC)
    return changed
