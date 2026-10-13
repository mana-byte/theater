"""Static Vibe writer and Theater reader store-format checks."""

from __future__ import annotations

import ast
from pathlib import Path

from .. import github
from ..contract import Check

UPSTREAM_STORE = "harness/runtimes/python/python/mistralai_vibe_local_harness/vibe/_storage.py"
READER_STORE = Path("theater/harness/builtin/plugins/vibe/unified_store_types.py")


def integer_assignment(source: str, name: str) -> int:
    for statement in ast.parse(source).body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in statement.targets
        ):
            value = ast.literal_eval(statement.value)
            if type(value) is int and value >= 0:
                return value
    raise ValueError(f"missing non-negative {name} assignment")


def reader_minor(root: Path) -> int:
    return integer_assignment((root / READER_STORE).read_text(), "STORE_FORMAT_MINOR")


def store_check(root: Path, version: str) -> Check:
    try:
        source = github.raw_file("mistralai/mistral-vibe", f"v{version}", UPSTREAM_STORE)
        upstream = integer_assignment(source, "STORE_FORMAT_MINOR")
        ours = reader_minor(root)
    except (OSError, ValueError, SyntaxError) as exc:
        return Check("vibe_store_format", "error", str(exc))
    if upstream > ours:
        return Check(
            "vibe_store_format",
            "fail",
            f"reader needs work: upstream store format {upstream} > ours {ours}",
        )
    return Check("vibe_store_format", "pass", f"upstream {upstream} <= reader {ours}")
