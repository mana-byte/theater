"""Pi gate parsing and single-value source bumps."""

from __future__ import annotations

import ast
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from ..contract import VerifiedSet, version_key
from ..gate_literals import assignments, replace_literal_span

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
    anchors: dict[str, tuple[int, int, int]] = {}
    excluded: tuple[tuple[int, int, int], ...] | None = None
    max_node: ast.Assign | None = None
    for assignment in assignments(
        source, (*_ANCHOR_NAMES, _EXCLUDED_NAME), filename=str(_CONSTANTS)
    ):
        if not isinstance(assignment.statement, ast.Assign):
            continue
        if assignment.name in _ANCHOR_NAMES:
            if assignment.name in anchors:
                raise ValueError(f"duplicate Pi gate anchor {assignment.name} in {_CONSTANTS}")
            value = assignment.literal()
            if not _is_triple(value):
                raise ValueError(
                    f"Pi gate anchor {assignment.name} must be a three-integer literal"
                )
            anchors[assignment.name] = cast(tuple[int, int, int], value)
            if assignment.name == "PI_FRONTEND_QUALIFIED_MAX":
                max_node = assignment.statement
        elif assignment.name == _EXCLUDED_NAME:
            if excluded is not None:
                raise ValueError(f"duplicate Pi gate anchor {assignment.name} in {_CONSTANTS}")
            value = assignment.literal()
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
    updated = replace_literal_span(
        source,
        node.value,
        str(current),
        str(new),
        f"Pi gate anchor has changed shape in {_CONSTANTS}",
        first_match=True,
    )
    (root / _CONSTANTS).write_text(updated, encoding="utf-8")
    return [_CONSTANTS]
