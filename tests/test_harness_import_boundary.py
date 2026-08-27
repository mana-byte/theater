"""Architecture test: production modules must not import built-in internals."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

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
    if (parts and parts[-1] == "__init__") or parts:
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


@pytest.mark.parametrize("path", _production_files(), ids=lambda p: str(p.relative_to(_ROOT)))
def test_no_cross_builtin_plugin_imports(path: Path) -> None:
    assert not _violations(path, path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("source", "path", "should_violate"),
    [
        (
            "from theater.harness.builtin.plugins.claude import parser",
            _PLUGINS_DIR / "codex" / "manifest.py",
            True,
        ),
        (
            "from theater.harness.builtin.plugins import claude",
            _PLUGINS_DIR / "codex" / "manifest.py",
            True,
        ),
        (
            "from ..claude import parser",
            _PLUGINS_DIR / "codex" / "manifest.py",
            True,
        ),
        (
            "from .parser import decode",
            _PLUGINS_DIR / "claude" / "manifest.py",
            False,
        ),
    ],
    ids=["absolute", "root-from", "relative-cross", "same-package"],
)
def test_boundary_detection(source: str, path: Path, should_violate: bool) -> None:
    violations = _violations(path, source)
    assert bool(violations) == should_violate
