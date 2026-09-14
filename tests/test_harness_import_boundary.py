"""Architecture test: production modules must not import built-in internals."""

from __future__ import annotations

import ast
import importlib.util
from functools import partial
from pathlib import Path

from tests.rig.tables import run_rows

_ROOT = Path(__file__).resolve().parents[1]
_THEATER = _ROOT / "theater"
_PLUGINS_DOTTED = "theater.harness.builtin.plugins"
_PLUGINS_DIR = _THEATER / "harness" / "builtin" / "plugins"


def _production_files() -> list[Path]:
    return [p for p in _THEATER.rglob("*.py") if "__pycache__" not in p.relative_to(_THEATER).parts]


def _harness_of(path: Path) -> str | None:
    try:
        rel = path.relative_to(_PLUGINS_DIR)
    except ValueError:
        return None
    return rel.parts[0] if rel.parts else None


def _dotted_package(path: Path) -> str:
    rel = path.relative_to(_THEATER).with_suffix("")
    parts = list(rel.parts)
    if parts:
        parts.pop()
    return "theater." + ".".join(parts) if parts else "theater"


def _resolve_name(node: ast.Import | ast.ImportFrom, owner_pkg: str) -> list[tuple[str, str]]:
    """Return (base, alias) pairs for one import node."""
    if isinstance(node, ast.Import):
        return [(a.name, "") for a in node.names]
    if node.level:
        pkg = importlib.util.resolve_name("." * node.level + (node.module or ""), owner_pkg)
        base = pkg if isinstance(pkg, str) else ""
    else:
        base = node.module or ""
    return [(base, a.name) for a in node.names]


def _target_harness(base: str, alias: str) -> str | None:
    """Harness name targeted by this import, or None if not a builtin plugin."""
    if base == _PLUGINS_DOTTED:
        return alias
    if base.startswith(_PLUGINS_DOTTED + "."):
        return base[len(_PLUGINS_DOTTED) + 1 :].split(".", maxsplit=1)[0]
    return None


def _violations(path: Path, source: str) -> list[str]:
    """Cross-boundary builtin-plugin imports in ``source`` resolved at ``path``."""
    tree = ast.parse(source)
    owner = _harness_of(path)
    owner_pkg = _dotted_package(path)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for base, alias in _resolve_name(node, owner_pkg):
            target = _target_harness(base, alias)
            if target is not None and target != owner:
                found.append(f"{base}.{alias}" if alias else base)
    return found


def test_no_cross_builtin_plugin_imports() -> None:
    """One repository-wide scan reporting every violating file at once."""
    problems: list[str] = []
    for path in _production_files():
        rel = path.relative_to(_ROOT)
        try:
            found = _violations(path, path.read_text(encoding="utf-8"))
        except Exception as exc:
            problems.append(f"{rel}: unreadable or unparsable: {exc!r}")
            continue
        problems.extend(f"{rel}: cross-boundary import {v}" for v in found)
    assert not problems, "\n".join(problems)


def test_boundary_detection() -> None:
    """Absolute, root-from and relative-cross imports violate; same-package clears."""
    cases = [
        (
            "absolute",
            "from theater.harness.builtin.plugins.claude import parser",
            _PLUGINS_DIR / "codex" / "manifest.py",
            True,
        ),
        (
            "root-from",
            "from theater.harness.builtin.plugins import claude",
            _PLUGINS_DIR / "codex" / "manifest.py",
            True,
        ),
        (
            "relative-cross",
            "from ..claude import parser",
            _PLUGINS_DIR / "codex" / "manifest.py",
            True,
        ),
        (
            "same-package",
            "from .parser import decode",
            _PLUGINS_DIR / "claude" / "manifest.py",
            False,
        ),
    ]

    def check(source: str, path: Path, should_violate: bool) -> None:
        assert bool(_violations(path, source)) == should_violate

    run_rows(
        (label, partial(check, source, path, flag))
        for label, source, path, flag in cases
    )
