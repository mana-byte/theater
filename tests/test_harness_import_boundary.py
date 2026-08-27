"""Architecture test: production modules must not import built-in internals."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_THEATER = _ROOT / "theater"
_BUILTIN_PLUGINS_DOTTED = "theater.harness.builtin.plugins"
_BUILTIN_PLUGINS_DIR = _THEATER / "harness" / "builtin" / "plugins"


def _all_production_py_files() -> list[Path]:
    """All .py under theater/ excluding __pycache__."""
    return [p for p in _THEATER.rglob("*.py") if "__pycache__" not in p.relative_to(_THEATER).parts]


def _dotted_package(path: Path) -> str:
    """The Python ``__package__`` a production file belongs to."""
    rel = path.relative_to(_THEATER)
    parts = list(rel.with_suffix("").parts)
    if not parts:
        return "theater"
    if parts[-1] == "__init__":
        parts.pop()
    else:
        parts.pop()
    return "theater." + ".".join(parts) if parts else "theater"


def _harness_of(path: Path) -> str | None:
    """Harness name if path is inside builtin/plugins/<harness>/."""
    try:
        rel = path.relative_to(_BUILTIN_PLUGINS_DIR)
    except ValueError:
        return None
    return rel.parts[0] if rel.parts else None


def _resolve_import(node: ast.Import | ast.ImportFrom, owner_pkg: str) -> set[str]:
    """Resolve an import node to fully-qualified dotted names."""
    names: set[str] = set()
    if isinstance(node, ast.Import):
        for alias in node.names:
            names.add(alias.name)
        return names
    # ImportFrom
    if node.level == 0:
        if node.module is None:
            return names
        base = node.module
    else:
        # Relative: resolve against owner_pkg parts.
        parts = owner_pkg.split(".")
        if node.level > len(parts):
            return names
        base_parts = parts[: len(parts) - node.level + 1]
        if node.module:
            base_parts.append(node.module)
        base = ".".join(base_parts)
    for alias in node.names:
        names.add(f"{base}.{alias.name}" if base else alias.name)
    return names


def _target_harness(name: str) -> str | None:
    """Harness name if name targets a builtin plugin package or submodule."""
    if name == _BUILTIN_PLUGINS_DOTTED:
        return ""  # root: aliases decide
    prefix = _BUILTIN_PLUGINS_DOTTED + "."
    if not name.startswith(prefix):
        return None
    return name[len(prefix) :].split(".", maxsplit=1)[0]


def _is_cross_boundary(name: str, owner_harness: str | None) -> bool:
    """True if name imports a different built-in's internals."""
    target = _target_harness(name)
    if target is None:
        return False
    if target == "":
        # from ...plugins import <harness> — cross-boundary if harness != owner.
        # We can't tell from name alone; caller resolves alias form separately.
        return False
    return target != owner_harness


def _check_file(path: Path) -> list[str]:
    """Return list of cross-boundary import violations in a production file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owner = _harness_of(path)
    owner_pkg = _dotted_package(path)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        resolved = _resolve_import(node, owner_pkg)
        for name in resolved:
            target = _target_harness(name)
            if target is None:
                continue
            if target == "":
                # from ...plugins import <harness> — check alias vs owner.
                if owner is not None:
                    alias_name = name.rsplit(".", 1)[-1]
                    if alias_name != owner:
                        violations.append(name)
                elif owner is None:
                    violations.append(name)
                continue
            if target != owner:
                violations.append(name)
    return violations


@pytest.mark.parametrize(
    "path",
    _all_production_py_files(),
    ids=lambda p: str(p.relative_to(_ROOT)),
)
def test_no_cross_builtin_plugin_imports(path: Path) -> None:
    """Production must not import another built-in's internals."""
    violations = _check_file(path)
    assert not violations, (
        f"{path.relative_to(_ROOT)} imports {violations!r}, "
        "a built-in plugin internal outside its own package"
    )


# ---- detector tests ------------------------------------------------------


def test_absolute_cross_boundary_rejected(tmp_path: Path) -> None:
    """Direct absolute import of another built-in is rejected."""
    f = _BUILTIN_PLUGINS_DIR / "codex" / "manifest.py"
    tree = ast.parse(f.read_text(encoding="utf-8"))
    owner = _harness_of(f)
    owner_pkg = _dotted_package(f)
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for name in _resolve_import(node, owner_pkg):
            if _is_cross_boundary(name, owner):
                found = True
    # codex/manifest.py does not actually import claude, so simulate one:
    fake = ast.parse("from theater.harness.builtin.plugins.claude import parser")
    for node in ast.walk(fake):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for name in _resolve_import(node, owner_pkg):
            if _is_cross_boundary(name, owner):
                found = True
    assert found


def test_from_plugins_root_rejected(tmp_path: Path) -> None:
    """`from ...plugins import claude` outside claude is rejected."""
    f = _BUILTIN_PLUGINS_DIR / "codex" / "manifest.py"
    owner = _harness_of(f)
    owner_pkg = _dotted_package(f)
    fake = ast.parse("from theater.harness.builtin.plugins import claude")
    for node in ast.walk(fake):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        resolved = _resolve_import(node, owner_pkg)
        for name in resolved:
            target = _target_harness(name)
            if target == "":
                alias_name = name.rsplit(".", 1)[-1]
                assert alias_name != owner
                continue
    # Also via _check_file simulation
    violations: list[str] = []
    for node in ast.walk(fake):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        resolved = _resolve_import(node, owner_pkg)
        for name in resolved:
            target = _target_harness(name)
            if target is None:
                continue
            if target == "":
                alias_name = name.rsplit(".", 1)[-1]
                if alias_name != owner:
                    violations.append(name)
                continue
            if target != owner:
                violations.append(name)
    assert violations


def test_relative_cross_plugin_rejected() -> None:
    """A Codex module doing `from ..claude import parser` is rejected."""
    f = _BUILTIN_PLUGINS_DIR / "codex" / "manifest.py"
    owner = _harness_of(f)
    owner_pkg = _dotted_package(f)
    fake = ast.parse("from ..claude import parser")
    for node in ast.walk(fake):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        resolved = _resolve_import(node, owner_pkg)
        for name in resolved:
            target = _target_harness(name)
            assert target == "claude"
            assert target != owner


def test_same_plugin_relative_allowed() -> None:
    """A Claude module doing `from .parser import decode` is allowed."""
    f = _BUILTIN_PLUGINS_DIR / "claude" / "manifest.py"
    owner = _harness_of(f)
    owner_pkg = _dotted_package(f)
    fake = ast.parse("from .parser import decode")
    violations: list[str] = []
    for node in ast.walk(fake):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        resolved = _resolve_import(node, owner_pkg)
        for name in resolved:
            target = _target_harness(name)
            if target is None:
                continue
            if target != owner:
                violations.append(name)
    assert not violations
