"""Pi gate parsing and single-value source bumps."""

from __future__ import annotations

import ast
from collections.abc import Sequence
from pathlib import Path

from ..contract import VerifiedSet, load_evidenced, version_key

_CONSTANTS = Path("theater/harness/builtin/plugins/pi/runtime_constants.py")
_GATE_NAMES = (
    "PI_FRONTEND_QUALIFIED_FLOOR",
    "PI_FRONTEND_QUALIFIED_HOLE",
    "PI_FRONTEND_QUALIFIED_RESUME",
    "PI_FRONTEND_QUALIFIED_MAX",
)


def _next_patch(version: str) -> str:
    major, minor, patch = version_key(version)
    return f"{major}.{minor}.{patch + 1}"


def _version(values: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in values)


def _gate_literals(root: Path) -> tuple[str, dict[str, tuple[int, int, int]], ast.Assign]:
    source = (root / _CONSTANTS).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_CONSTANTS))
    values: dict[str, tuple[int, int, int]] = {}
    max_node: ast.Assign | None = None
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id not in _GATE_NAMES:
            continue
        if target.id in values:
            raise ValueError(f"duplicate Pi gate anchor {target.id} in {_CONSTANTS}")
        value = ast.literal_eval(node.value)
        if (
            not isinstance(value, tuple)
            or len(value) != 3
            or any(type(part) is not int or part < 0 for part in value)
        ):
            raise ValueError(f"Pi gate anchor {target.id} must be a three-integer literal")
        values[target.id] = value
        if target.id == "PI_FRONTEND_QUALIFIED_MAX":
            max_node = node
    if set(values) != set(_GATE_NAMES) or max_node is None:
        raise ValueError(f"missing Pi gate anchor in {_CONSTANTS}")
    return source, values, max_node


def verified(root: Path) -> VerifiedSet:
    _, values, _ = _gate_literals(root)
    floor, bad, resume, upper = (_version(values[name]) for name in _GATE_NAMES)
    if _next_patch(bad) != resume:
        raise ValueError(f"Pi compatibility gap is not one excluded patch: {bad} to {resume}")
    return VerifiedSet(
        "range",
        floor=floor,
        ceiling=_next_patch(upper),
        known_bad=frozenset({bad}),
        evidenced=load_evidenced(root, "pi"),
    )


def apply_bump(root: Path, greens: Sequence[str]) -> list[Path]:
    source, values, node = _gate_literals(root)
    verified(root)
    ordered = list(dict.fromkeys(greens))
    if not ordered:
        return []
    if ordered != sorted(ordered, key=version_key):
        raise ValueError("Pi green releases must be stable and ascending")

    current = values["PI_FRONTEND_QUALIFIED_MAX"]
    new = version_key(ordered[-1])
    if new <= current:
        return []
    if node.lineno != node.end_lineno:
        raise ValueError(f"Pi gate anchor must occupy one line in {_CONSTANTS}")
    lines = source.splitlines(keepends=True)
    old_line = lines[node.lineno - 1]
    old_value = old_line[node.value.col_offset : node.value.end_col_offset]
    if old_value != str(current):
        raise ValueError(f"Pi gate anchor has changed shape in {_CONSTANTS}")
    lines[node.lineno - 1] = old_line.replace(old_value, str(new), 1)
    (root / _CONSTANTS).write_text("".join(lines), encoding="utf-8")
    return [_CONSTANTS]
