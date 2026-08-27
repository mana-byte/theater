"""Architecture test: production modules must not import built-in internals.

A module outside ``theater/harness/builtin/plugins/<harness>/`` must not
import internals under ``theater.harness.builtin.plugins.<harness>``. Tests
and docs may name/import built-ins. The guard also rejects one built-in
importing another.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_THEATER = _ROOT / "theater"
_BUILTIN_PLUGINS_PREFIX = "theater.harness.builtin.plugins."
_BUILTIN_PLUGINS_DIR = _THEATER / "harness" / "builtin" / "plugins"


def _all_production_py_files() -> list[Path]:
    """Return all .py files under theater/ except tests and __pycache__."""
    files: list[Path] = []
    for path in _THEATER.rglob("*.py"):
        rel = path.relative_to(_THEATER)
        if "__pycache__" in rel.parts:
            continue
        files.append(path)
    return files


def _imported_names(tree: ast.Module) -> set[str]:
    """Return fully-qualified module names imported by ``tree``."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
    return names


def _harness_of(path: Path) -> str | None:
    """Return the harness package name if ``path`` is inside a built-in package."""
    try:
        rel = path.relative_to(_BUILTIN_PLUGINS_DIR)
    except ValueError:
        return None
    if not rel.parts:
        return None
    return rel.parts[0]


def _is_builtin_plugin_import(name: str, owner_harness: str | None) -> bool:
    """True if ``name`` is a built-in plugin internal import.

    Imports of ``theater.harness.builtin.plugins.<harness>`` from outside
    that same harness package are forbidden. Imports of the loading layer,
    contracts, or other non-internal modules are exempt.
    """
    if not name.startswith(_BUILTIN_PLUGINS_PREFIX):
        return False
    # Strip prefix to get the target harness name.
    remainder = name[len(_BUILTIN_PLUGINS_PREFIX) :]
    target_harness = remainder.split(".")[0]
    # Same-harness imports are allowed; everything else is cross-boundary.
    return not (owner_harness is not None and target_harness == owner_harness)


@pytest.mark.parametrize(
    "path",
    _all_production_py_files(),
    ids=lambda p: str(p.relative_to(_ROOT)),
)
def test_no_cross_builtin_plugin_imports(path: Path) -> None:
    """A production module must not import another built-in's internals."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owner = _harness_of(path)
    for name in _imported_names(tree):
        assert not _is_builtin_plugin_import(name, owner), (
            f"{path.relative_to(_ROOT)} imports {name!r}, "
            "which is a built-in plugin internal outside its own package"
        )
