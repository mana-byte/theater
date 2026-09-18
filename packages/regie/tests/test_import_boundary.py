import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[1] / "src" / "regie"


def test_regie_production_imports_only_the_public_frontend_surface() -> None:
    violations: list[str] = []
    source_text: list[str] = []
    for path in SOURCE_ROOT.rglob("*.py"):
        text = path.read_text()
        source_text.append(text)
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                names = [node.module]
            else:
                continue
            for name in names:
                if _private_theater_import(name):
                    violations.append(f"{path.relative_to(SOURCE_ROOT)}: {name}")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            if not _is_dynamic_import(node.func):
                continue
            target = node.args[0]
            if (
                isinstance(target, ast.Constant)
                and isinstance(target.value, str)
                and _private_theater_import(target.value)
            ):
                violations.append(f"{path.relative_to(SOURCE_ROOT)}: {target.value}")

    assert violations == []
    assert "frontend.participants.tree" not in "\n".join(source_text)


def _private_theater_import(name: str) -> bool:
    return name == "theater" or (
        name.startswith("theater.") and not name.startswith("theater.frontend")
    )


def _is_dynamic_import(function: ast.expr) -> bool:
    return (isinstance(function, ast.Name) and function.id in {"__import__", "import_module"}) or (
        isinstance(function, ast.Attribute)
        and function.attr == "import_module"
        and isinstance(function.value, ast.Name)
        and function.value.id == "importlib"
    )
