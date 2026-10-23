"""AST assignment facts and checked source edits for compatibility gates."""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LiteralAssignment:
    name: str
    statement: ast.Assign | ast.AnnAssign
    value: ast.expr

    def literal(self) -> object:
        return ast.literal_eval(self.value)


def assignments(
    source: str, names: Collection[str], *, filename: str = "<unknown>"
) -> Iterator[LiteralAssignment]:
    """Yield top-level simple or annotated assignments; callers choose accepted shapes."""
    for statement in ast.parse(source, filename=filename).body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target, value = statement.targets[0], statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            target, value = statement.target, statement.value
        else:
            continue
        if isinstance(target, ast.Name) and target.id in names:
            yield LiteralAssignment(target.id, statement, value)


def replace_tuple_line(
    source: str,
    name: str,
    value: tuple[int, int, int] | Callable[[], tuple[int, int, int]],
    error: str,
) -> str:
    """Replace the first exact line; a deferred value preserves anchor-first errors."""
    match = re.search(rf"(?m)^{name} = \(\d+, \d+, \d+\)$", source)
    if match is None:
        raise ValueError(error)
    value = value() if callable(value) else value
    return source[: match.start()] + f"{name} = {value}" + source[match.end() :]


def replace_literal_span(
    source: str,
    value: ast.expr,
    expected: str,
    replacement: str,
    error: str,
    *,
    ascii_only: bool = False,
    first_match: bool = False,
) -> str:
    """Check a parsed literal's source span and replace one line by the caller's rule."""
    lines = source.splitlines(keepends=True)
    line = lines[value.lineno - 1]
    start, end = value.col_offset, value.end_col_offset
    if (ascii_only and not line.isascii()) or line[start:end] != expected:
        raise ValueError(error)
    lines[value.lineno - 1] = (
        line.replace(expected, replacement, 1)
        if first_match
        else f"{line[:start]}{replacement}{line[end:]}"
    )
    return "".join(lines)
