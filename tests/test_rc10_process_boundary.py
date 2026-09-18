"""Static ownership checks for the Theater/Régie process boundary."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]
THEATER = ROOT / "theater"
REGIE = ROOT / "packages" / "regie"


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.append(node.module)
        elif isinstance(node, ast.Call) and node.args and _dynamic_import(node.func):
            target = node.args[0]
            if isinstance(target, ast.Constant) and isinstance(target.value, str):
                imports.append(target.value)
    return imports


def _dynamic_import(function: ast.expr) -> bool:
    return (isinstance(function, ast.Name) and function.id in {"__import__", "import_module"}) or (
        isinstance(function, ast.Attribute)
        and function.attr == "import_module"
        and isinstance(function.value, ast.Name)
        and function.value.id == "importlib"
    )


def test_obsolete_theater_tmux_and_regie_packages_are_retired() -> None:
    assert not list((THEATER / "tmux").glob("*.py"))
    assert not list((THEATER / "regie").rglob("*.py"))
    violations = [
        f"{path.relative_to(ROOT)}: {name}"
        for path in THEATER.rglob("*.py")
        for name in _imports(path)
        if name == "theater.tmux"
        or name.startswith("theater.tmux.")
        or name == "theater.regie"
        or name.startswith("theater.regie.")
    ]
    assert violations == []


def test_regie_production_and_tests_do_not_hide_private_theater_imports() -> None:
    violations: list[str] = []
    for root in (REGIE / "src" / "regie", REGIE / "tests"):
        for path in root.rglob("*.py"):
            for name in _imports(path):
                if name == "theater" or (
                    name.startswith("theater.") and not name.startswith("theater.frontend")
                ):
                    violations.append(f"{path.relative_to(ROOT)}: {name}")
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                if not _is_string_patch(node.func):
                    continue
                target = node.args[0]
                if (
                    isinstance(target, ast.Constant)
                    and isinstance(target.value, str)
                    and target.value.startswith("theater.")
                    and not target.value.startswith("theater.frontend")
                ):
                    violations.append(f"{path.relative_to(ROOT)}: patch target {target.value}")
    assert violations == []


def _is_string_patch(function: ast.expr) -> bool:
    if isinstance(function, ast.Name):
        return function.id in {"patch", "patch_object"}
    if not isinstance(function, ast.Attribute):
        return False
    if function.attr == "patch":
        return True
    return (
        function.attr == "setattr"
        and isinstance(function.value, ast.Name)
        and (function.value.id == "monkeypatch")
    )


def test_daemon_has_no_tmux_subprocess_execution_path() -> None:
    forbidden = ('subprocess.run(["tmux"', 'create_subprocess_exec("tmux"', 'execvp("tmux"')
    violations = [
        str(path.relative_to(ROOT))
        for path in (THEATER / "daemon").rglob("*.py")
        if any(marker in path.read_text() for marker in forbidden)
    ]
    assert violations == []
