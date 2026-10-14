"""Advance Vibe's exclusive ceiling to the patch after the last green."""

from __future__ import annotations

import ast
import re
from collections.abc import Sequence
from pathlib import Path

from ..contract import version_key

COMPAT = Path("theater/harness/builtin/plugins/vibe/compatibility.py")
COMPAT_TEST = Path("tests/test_vibe_compatibility.py")
DOC = Path("docs/native-interaction/vibe.md")
LAUNCH_TEST = Path("tests/test_vibe_launch_safety.py")


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
    major, minor, patch = keys[-1]
    ceiling_key = (major, minor, patch + 1)
    new_ceiling = ".".join(map(str, ceiling_key))
    if version_key(new_ceiling) <= version_key(old_ceiling):
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
    compat_updated = compat_updated.replace(
        "#: The newest Vibe release line whose unified session store this reader\n"
        "#: understands: 2.26.x writes store_format_minor 8 (vibe 376f6a33). The probe is\n"
        "#: display-only, so launch planning enforces the same ceiling (launch.plan_launch);\n"
        "#: bump the ceiling in the same commit as unified_store_types.STORE_FORMAT_MINOR —\n"
        "#: the qualified range once outran the reader, and finished jobs waited forever on\n"
        "#: stores no reader could open.",
        "#: The ceiling is the first stable release not yet proved compatible.\n"
        "#: Launch planning enforces it because the probe is display-only.",
    )
    compat_updated = compat_updated.replace(
        "#: Exclusive qualified boundary chosen from upstream release discovery.\n"
        "#: Launch planning enforces this ceiling because the probe is display-only.",
        "#: The ceiling is the first stable release not yet proved compatible.\n"
        "#: Launch planning enforces it because the probe is display-only.",
    )

    compat_test_source = (root / COMPAT_TEST).read_text()
    compat_test_updated = compat_test_source.replace(
        f">={floor},<{old_ceiling}", f">={floor},<{new_ceiling}"
    )
    compat_test_updated = compat_test_updated.replace(
        f'("vibe {old_ceiling}", False, "{old_ceiling}",',
        f'("vibe {new_ceiling}", False, "{new_ceiling}",',
    )
    compat_test_updated = compat_test_updated.replace(
        f"== {version_key(old_ceiling)}", f"== {ceiling_key}"
    )
    compat_test_updated = re.sub(
        r"(?:2\.26\.x writes store_format_minor 8 \(vibe 376f6a33\); the ceiling is the next\n"
        r"    release line, and the reader understands exactly that minor\.|"
        r"Verified releases through \d+\.\d+\.\d+ write a store the reader understands;\n"
        r"    the next patch is held for verification\.)",
        "Verified Vibe releases write a store the reader understands;\n"
        "    the exclusive ceiling is held for verification.",
        compat_test_updated,
    )

    launch_source = (root / LAUNCH_TEST).read_text()
    old_tuple = ", ".join(map(str, version_key(old_ceiling)))
    new_tuple = ", ".join(map(str, ceiling_key))
    launch_anchor = f'@pytest.mark.parametrize("version", [({old_tuple}), (3, 0, 0)])'
    if launch_anchor not in launch_source:
        raise ValueError("Vibe launch-safety ceiling parameter anchor missing")
    launch_updated = launch_source.replace(
        launch_anchor,
        f'@pytest.mark.parametrize("version", [({new_tuple}), (3, 0, 0)])',
        1,
    )

    doc = root / DOC
    doc_source: str | None = None
    doc_updated: str | None = None
    if doc.exists():
        doc_source = doc.read_text()
        old = f"Vibe compatibility range: >={floor},<{old_ceiling}."
        new = f"Vibe compatibility range: >={floor},<{new_ceiling}."
        if old in doc_source:
            doc_updated = doc_source.replace(old, new)
        else:
            marker = "## Current baseline\n"
            if marker not in doc_source:
                raise ValueError("Vibe docs baseline section missing")
            doc_updated = doc_source.replace(marker, f"{marker}\n{new}\n", 1)

    updates = (
        (COMPAT, compat_source, compat_updated),
        (COMPAT_TEST, compat_test_source, compat_test_updated),
        (LAUNCH_TEST, launch_source, launch_updated),
        (DOC, doc_source, doc_updated),
    )
    changed: list[Path] = []
    for relative, source, updated in updates:
        if source is not None and updated is not None and source != updated:
            (root / relative).write_text(updated)
            changed.append(relative)
    return changed
