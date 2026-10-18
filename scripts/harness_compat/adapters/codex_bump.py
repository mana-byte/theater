"""Codex gate parsing and the ceiling-only source bump."""

from __future__ import annotations

import ast
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..contract import VerifiedSet, load_evidenced, version_key

PLAN = Path("theater/harness/builtin/plugins/codex/runtime_plan.py")
_FLOOR = "CODEX_RUNTIME_QUALIFIED_FLOOR"
_CEILING = "CODEX_RUNTIME_QUALIFIED_CEILING"
_EXCLUDED = "CODEX_RUNTIME_QUALIFIED_EXCLUDED"

Triple = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class _Gate:
    source: str
    floor: Triple
    ceiling: Triple
    excluded: tuple[Triple, ...]
    ceiling_node: ast.expr


def _triple(value: object, name: str) -> Triple:
    if (
        not isinstance(value, tuple)
        or len(value) != 3
        or not all(type(part) is int and part >= 0 for part in value)
    ):
        raise ValueError(f"Codex gate constant {name} in {PLAN} must be a three-integer literal")
    return (value[0], value[1], value[2])


def _triples(value: object, name: str) -> tuple[Triple, ...]:
    entries = value if isinstance(value, tuple) else None
    if entries is None:
        raise ValueError(f"Codex gate constant {name} in {PLAN} must be a tuple literal")
    return tuple(_triple(entry, name) for entry in entries)


def _text(triple: Triple) -> str:
    return ".".join(str(part) for part in triple)


def _gate(root: Path) -> _Gate:
    """Parse the gate constants; a missing, duplicated or non-literal one fails closed."""
    source = (root / PLAN).read_text(encoding="utf-8")
    found: dict[str, ast.expr] = {}
    for statement in ast.parse(source, filename=str(PLAN)).body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target, value = statement.targets[0], statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            target, value = statement.target, statement.value
        else:
            continue
        if isinstance(target, ast.Name) and target.id in (_FLOOR, _CEILING, _EXCLUDED):
            if target.id in found:
                raise ValueError(f"duplicate Codex gate constant {target.id} in {PLAN}")
            found[target.id] = value
    if set(found) != {_FLOOR, _CEILING, _EXCLUDED}:
        raise ValueError(f"missing Codex gate constant in {PLAN}")
    return _Gate(
        source,
        _triple(ast.literal_eval(found[_FLOOR]), _FLOOR),
        _triple(ast.literal_eval(found[_CEILING]), _CEILING),
        _triples(ast.literal_eval(found[_EXCLUDED]), _EXCLUDED),
        found[_CEILING],
    )


def verified(root: Path) -> VerifiedSet:
    gate = _gate(root)
    return VerifiedSet(
        "range",
        floor=_text(gate.floor),
        ceiling=_text(gate.ceiling),
        known_bad=frozenset(_text(entry) for entry in gate.excluded),
        evidenced=load_evidenced(root, "codex"),
    )


def apply_bump(root: Path, greens: Sequence[str]) -> list[Path]:
    """Move the exclusive ceiling past the newest green; the only line this ever edits."""
    if not greens:
        return []
    keys = [version_key(version) for version in greens]
    if keys != sorted(set(keys)):
        raise ValueError("Codex greens must be unique and ascending")
    gate = _gate(root)
    if keys[0] < gate.floor:
        raise ValueError("Codex green is below the qualified floor")
    major, minor, patch = keys[-1]
    new = (major, minor, patch + 1)
    if new <= gate.ceiling:
        return []
    node = gate.ceiling_node
    if node.lineno != node.end_lineno or node.end_col_offset is None:
        raise ValueError(f"Codex ceiling must occupy one line in {PLAN}")
    lines = gate.source.splitlines(keepends=True)
    line = lines[node.lineno - 1]
    start, end = node.col_offset, node.end_col_offset
    if not line.isascii() or line[start:end] != str(gate.ceiling):
        raise ValueError(f"Codex ceiling has changed shape in {PLAN}")
    lines[node.lineno - 1] = f"{line[:start]}{new}{line[end:]}"
    (root / PLAN).write_text("".join(lines), encoding="utf-8")
    return [PLAN]
