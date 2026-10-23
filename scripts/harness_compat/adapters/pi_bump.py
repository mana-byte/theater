"""Pi gate parsing and single-value source bumps."""

from __future__ import annotations

import ast
from collections.abc import Sequence
from pathlib import Path

from ..contract import VerifiedSet, version_key

_CONSTANTS = Path("theater/harness/builtin/plugins/pi/runtime_constants.py")
_ANCHOR_NAMES = ("PI_FRONTEND_QUALIFIED_FLOOR", "PI_FRONTEND_QUALIFIED_MAX")
_EXCLUDED_NAME = "PI_FRONTEND_QUALIFIED_EXCLUDED"


def _next_patch(version: str) -> str:
    major, minor, patch = version_key(version)
    return f"{major}.{minor}.{patch + 1}"


def _version(values: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in values)


def _is_triple(value: object) -> bool:
    return (
        isinstance(value, tuple)
        and len(value) == 3
        and all(type(part) is int and part >= 0 for part in value)
    )


class _GateLiterals:
    """Parsed Pi gate anchors: the two range triples, the excluded triples, the MAX node."""

    def __init__(
        self,
        source: str,
        anchors: dict[str, tuple[int, int, int]],
        excluded: tuple[tuple[int, int, int], ...],
        max_node: ast.Assign,
    ) -> None:
        self.source = source
        self.anchors = anchors
        self.excluded = excluded
        self.max_node = max_node


def _gate_literals(root: Path) -> _GateLiterals:
    source = (root / _CONSTANTS).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_CONSTANTS))
    anchors: dict[str, tuple[int, int, int]] = {}
    excluded: tuple[tuple[int, int, int], ...] | None = None
    max_node: ast.Assign | None = None
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if target.id in _ANCHOR_NAMES:
            if target.id in anchors:
                raise ValueError(f"duplicate Pi gate anchor {target.id} in {_CONSTANTS}")
            value = ast.literal_eval(node.value)
            if not _is_triple(value):
                raise ValueError(f"Pi gate anchor {target.id} must be a three-integer literal")
            anchors[target.id] = value
            if target.id == "PI_FRONTEND_QUALIFIED_MAX":
                max_node = node
        elif target.id == _EXCLUDED_NAME:
            if excluded is not None:
                raise ValueError(f"duplicate Pi gate anchor {target.id} in {_CONSTANTS}")
            value = ast.literal_eval(node.value)
            if not isinstance(value, tuple) or not all(_is_triple(entry) for entry in value):
                raise ValueError(
                    f"Pi gate anchor {_EXCLUDED_NAME} must be a tuple of three-integer literals"
                )
            excluded = value
    if set(anchors) != set(_ANCHOR_NAMES) or excluded is None or max_node is None:
        raise ValueError(f"missing Pi gate anchor in {_CONSTANTS}")
    return _GateLiterals(source, anchors, excluded, max_node)


def verified(root: Path) -> VerifiedSet:
    gate = _gate_literals(root)
    return VerifiedSet(
        "range",
        floor=_version(gate.anchors["PI_FRONTEND_QUALIFIED_FLOOR"]),
        ceiling=_next_patch(_version(gate.anchors["PI_FRONTEND_QUALIFIED_MAX"])),
        known_bad=frozenset(_version(entry) for entry in gate.excluded),
    )


def apply_bump(root: Path, greens: Sequence[str]) -> list[Path]:
    gate = _gate_literals(root)
    verified(root)
    source, node = gate.source, gate.max_node
    ordered = list(dict.fromkeys(greens))
    if not ordered:
        return []
    if ordered != sorted(ordered, key=version_key):
        raise ValueError("Pi green releases must be stable and ascending")

    current = gate.anchors["PI_FRONTEND_QUALIFIED_MAX"]
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
