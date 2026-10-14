"""The files that decide a harness verdict: any change to them invalidates reused evidence."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

from .contract import EVIDENCE_ROOT, Adapter

#: Framework modules whose behaviour decides a verdict.
CORE = (
    "contract",
    "framework",
    "probe",
    "process",
    "cleanup",
    "inputs",
    "runner",
    "sandbox",
    "mock",
    "select",
    "stale",
)
_FOLLOWED = ("tests", "scripts", "theater.harness")
_PLUGINS = Path("theater/harness/builtin/plugins")


def _module_file(root: Path, dotted: str) -> Path | None:
    base = root.joinpath(*dotted.split("."))
    return next((c for c in (base.with_suffix(".py"), base / "__init__.py") if c.is_file()), None)


def _imported(root: Path, path: Path) -> set[Path]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level and path.is_relative_to(root):
            package = ".".join(path.relative_to(root).parent.parts[: -(node.level - 1) or None])
            module = f"{package}.{node.module}" if node.module else package
            names.add(module)
            names.update(f"{module}.{alias.name}" for alias in node.names)
    wanted = (n for n in names if any(n == f or n.startswith(f"{f}.") for f in _FOLLOWED))
    files = {item for item in (_module_file(root, name) for name in wanted) if item is not None}
    for item in list(files):  # importing a module also runs its packages' __init__ files
        for directory in item.parents:
            if directory == root or not directory.is_relative_to(root):
                break
            if (directory / "__init__.py").is_file():
                files.add(directory / "__init__.py")
    return files


def _closure(root: Path, seeds: set[Path]) -> set[Path]:
    """The seeds plus every test, script or harness module they import, transitively."""
    seen: set[Path] = set()
    pending = [item for item in seeds if item.is_file()]
    while pending:
        path = pending.pop()
        if path not in seen:
            seen.add(path)
            pending.extend(_imported(root, path) - seen)
    return seen


def _conftests(root: Path, files: set[Path]) -> set[Path]:
    found: set[Path] = set()
    for path in files:
        directory = path.parent
        while directory.is_relative_to(root):
            if (directory / "conftest.py").is_file():
                found.add(directory / "conftest.py")
            if directory == root:
                break
            directory = directory.parent
    return found


def _seed_tests(root: Path, adapter: Adapter) -> set[Path]:
    files: set[Path] = set()
    for selector in (*adapter.green_tests, *adapter.consistency_tests):
        path = root / selector.split("::", 1)[0].split("[", 1)[0]
        if path.is_file():
            files.add(path)
        elif path.is_dir():
            files.update(path.rglob("*.py"))
    return files


def _tree(directory: Path) -> set[Path]:
    if not directory.is_dir():
        return set()
    return {
        item for item in directory.rglob("*") if item.is_file() and "__pycache__" not in item.parts
    }


def qualification_inputs(root: Path, harness: str, adapter: Adapter | None = None) -> set[Path]:
    """Framework core, adapter files, proof tests with their helpers and conftests, the plugin."""
    base = root / "scripts/harness_compat"
    adapters = sorted((base / "adapters").glob("*.py"))
    package = base / "adapters" / harness
    if package.is_dir():
        adapters.extend(sorted(package.rglob("*.py")))
    if not any(path.stem == harness or path.stem.startswith(f"{harness}_") for path in adapters):
        raise FileNotFoundError(f"adapter source absent: {harness}")
    paths = {base / f"{name}.py" for name in CORE} | set(adapters)
    paths |= set((root / "tests/native").glob("*.py"))
    paths |= _tree(root / _PLUGINS / harness)
    if adapter is not None:
        tests = _closure(root, _seed_tests(root, adapter))
        paths |= tests | _conftests(root, tests)
        fixtures = root / "tests/fixtures"
        if fixtures.is_dir():
            for entry in fixtures.glob(f"*{adapter.name}*"):
                paths |= {entry} if entry.is_file() else _tree(entry)
    evidence = root / EVIDENCE_ROOT
    return {item for item in paths if item.is_file() and not item.is_relative_to(evidence)}


def adapter_digest(root: Path, harness: str, adapter: Adapter | None = None) -> str:
    """Hash every qualification input by path and content; evidence files are outputs."""
    digest = hashlib.sha256()
    for path in sorted(qualification_inputs(root, harness, adapter)):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
