"""Read the OpenCode source gate and bump its single v1 ceiling literal."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import cast

from ..contract import VerifiedSet, version_key

RUNTIME_PLAN = Path("theater/harness/builtin/plugins/opencode/runtime_plan.py")
COUPLED_FILES: tuple[Path, ...] = (RUNTIME_PLAN,)
_FLOOR = "OPENCODE_TUI_MIN_VERSION"
_CEILING = "OPENCODE_TUI_MAX_VERSION"


def render_release(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def previous_patch(version: str) -> str:
    """The last admitted release below an exclusive ceiling."""
    major, minor, patch = version_key(version)
    if patch == 0:
        raise ValueError(f"no previous patch release for {version!r}")
    return f"{major}.{minor}.{patch - 1}"


def next_minor(version: str) -> str:
    """The first release of the next minor line."""
    major, minor, _ = version_key(version)
    return f"{major}.{minor + 1}.0"


def next_patch(version: str) -> str:
    """The exclusive ceiling after admitting a release."""
    major, minor, patch = version_key(version)
    return f"{major}.{minor}.{patch + 1}"


def read_tuples(root: Path, names: set[str]) -> dict[str, tuple[int, int, int]]:
    """Read AST tuple literals from plugin source without importing theater."""
    source = (root / RUNTIME_PLAN).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(RUNTIME_PLAN))
    gate: dict[str, tuple[int, int, int]] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id not in names:
            continue
        if target.id in gate or not isinstance(node.value, ast.Tuple):
            raise ValueError(f"the OpenCode gate is unreadable: {target.id} in {RUNTIME_PLAN}")
        values = node.value.elts
        if len(values) != 3 or any(
            not isinstance(value, ast.Constant) or type(value.value) is not int for value in values
        ):
            raise ValueError(f"the OpenCode gate is unreadable: {target.id} in {RUNTIME_PLAN}")
        gate[target.id] = cast(tuple[int, int, int], ast.literal_eval(node.value))
    if set(gate) != names:
        raise ValueError(f"the OpenCode gate is unreadable: missing tuple in {RUNTIME_PLAN}")
    return gate


def read_gate(root: Path) -> tuple[str, str]:
    """The v1 floor and exclusive ceiling as release text."""
    gate = read_tuples(root, {_FLOOR, _CEILING})
    if gate[_CEILING] <= gate[_FLOOR] or gate[_CEILING][2] == 0:
        raise ValueError("the OpenCode v1 gate has an invalid exclusive ceiling")
    return render_release(gate[_FLOOR]), render_release(gate[_CEILING])


def rewrite_ceiling(root: Path, name: str, green: str) -> list[Path]:
    """Point ``name`` at the exclusive ceiling right after ``green``; one source line changes."""
    path = root / RUNTIME_PLAN
    original = path.read_text(encoding="utf-8")
    match = re.search(rf"^{name} = \(\d+, \d+, \d+\)$", original, re.M)
    if match is None:
        raise ValueError(f"bump anchor missing in {RUNTIME_PLAN}: {name}")
    major, minor, patch = version_key(next_patch(green))
    bumped = (
        original[: match.start()]
        + f"{name} = ({major}, {minor}, {patch})"
        + original[match.end() :]
    )
    path.write_text(bumped, encoding="utf-8")
    return [RUNTIME_PLAN]


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
    if any(key[0] != 1 for key in keys):
        raise ValueError(f"the OpenCode v1 adapter only admits 1.x releases: {greens}")
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
