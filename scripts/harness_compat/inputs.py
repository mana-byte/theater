"""The files that decide a harness verdict: any change to them invalidates reused evidence."""

from __future__ import annotations

import ast
import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .contract import Adapter
from .gate_literals import assignments

_PROVIDER = Path("packages/regie/src")
_TRACKED = ("tests", "scripts", "theater", _PROVIDER.as_posix())
_PLUGINS = Path("theater/harness/builtin/plugins")
#: Selecting any of these makes the live conformance suite part of the proof.
_SUITE = "tests/conformance/live"
#: What the suite's daemon and bridge processes start from; the test process never imports them.
_DAEMON_ENTRIES = ("theater/cli/__main__.py", "theater/daemon/server.py")
_PROVIDER_ENTRIES = ("regie/__main__.py", "regie/entrypoint.py", "regie/bridge")
#: Régie's terminal UI: it renders what the bridge reports and changes no verdict, so it is
#: left out of the exercised closure.
_PROVIDER_UI = (
    "regie/app.py",
    "regie/app_parts",
    "regie/controllers",
    "regie/dashboard",
    "regie/render",
    "regie/motions",
    "regie/ambience",
    "regie/palette.py",
    "regie/presentation.py",
    "regie/bus_view.py",
    "regie/formatting.py",
)
#: Files the daemon and the suite's probe read at runtime without importing them.
_RUNTIME_DATA = (
    "theater/daemon/migrations",
    "theater/skills/builtin",
    "theater/mcp_plugins/builtin",
    "tests/support/compat_probe",
)


@dataclass(frozen=True)
class AdapterMetadata:
    plugin: str
    artifact_roots: tuple[Path, ...]


def adapter_metadata(adapter: Adapter | None, harness: str) -> AdapterMetadata:
    """Normalize the two optional adapter declarations with their established defaults."""
    return AdapterMetadata(
        plugin=getattr(adapter, "plugin", harness),
        artifact_roots=tuple(getattr(adapter, "artifact_roots", ())),
    )


def _bases(root: Path) -> tuple[Path, ...]:
    """Where an import resolves from: the root plus pytest's configured ``pythonpath``."""
    try:
        config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        extra = config["tool"]["pytest"]["ini_options"]["pythonpath"]
    except (OSError, ValueError, KeyError, TypeError):
        extra = []
    return (root, *(root / str(item) for item in extra), root / _PROVIDER)


def _module_file(bases: tuple[Path, ...], dotted: str) -> Path | None:
    for base in bases:
        stem = base.joinpath(*dotted.split("."))
        for candidate in (stem.with_suffix(".py"), stem / "__init__.py"):
            if candidate.is_file():
                return candidate
    return None


def _imported(
    root: Path, path: Path, bases: tuple[Path, ...], skip: tuple[Path, ...] = ()
) -> set[Path]:
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
    found = (_module_file(bases, name) for name in names)
    files = {
        item
        for item in found
        if item
        and any(item.is_relative_to(root / t) for t in _TRACKED)
        and not any(item.is_relative_to(left_out) for left_out in skip)
    }
    for item in list(files):  # importing a module also runs its packages' __init__ files
        for directory in item.parents:
            if directory == root or not directory.is_relative_to(root):
                break
            if (directory / "__init__.py").is_file():
                files.add(directory / "__init__.py")
    return files


def _conftests(root: Path, path: Path) -> set[Path]:
    found: set[Path] = set()
    directory = path.parent
    while directory.is_relative_to(root):
        if (directory / "conftest.py").is_file():
            found.add(directory / "conftest.py")
        if directory == root:
            break
        directory = directory.parent
    return found


def _closure(root: Path, seeds: set[Path], skip: tuple[Path, ...] = ()) -> set[Path]:
    """Seeds plus their conftests and every tracked module they import, to a fixed point.

    Paths under ``skip`` are never followed, even when imported.
    """
    bases = _bases(root)
    seen: set[Path] = set()
    pending = [item for item in seeds if item.is_file()]
    while pending:
        path = pending.pop()
        if path not in seen:
            seen.add(path)
            pending.extend((_imported(root, path, bases, skip) | _conftests(root, path)) - seen)
    return seen


def _seed_tests(root: Path, adapter: Adapter) -> set[Path]:
    files: set[Path] = set()
    for selector in (*adapter.green_tests, *adapter.consistency_tests):
        path = root / selector.split("::", 1)[0].split("[", 1)[0]
        if path.is_file():
            files.add(path)
        elif path.is_dir():
            files.update(path.rglob("*.py"))
    return files


def _suite_selected(adapter: Adapter | None) -> bool:
    selectors = (*adapter.green_tests, *adapter.consistency_tests) if adapter else ()
    return any(selector.startswith(_SUITE) for selector in selectors)


def _suite_processes(root: Path) -> set[Path]:
    """What the live suite exercises without importing it: the daemon and bridge it starts."""
    skip = tuple(root / _PROVIDER / item for item in _PROVIDER_UI)
    seeds = {root / item for item in _DAEMON_ENTRIES}
    for item in _PROVIDER_ENTRIES:
        path = root / _PROVIDER / item
        seeds |= {path} if path.is_file() else set(path.rglob("*.py"))
    paths = _closure(root, seeds, skip)
    for item in _RUNTIME_DATA:
        paths |= _tree(root / item)
    return paths


def _tree(directory: Path) -> set[Path]:
    if not directory.is_dir():
        return set()
    return {
        item
        for item in directory.rglob("*")
        if item.is_file() and "__pycache__" not in item.parts and not item.name.startswith(".")
    }


def qualification_inputs(root: Path, harness: str, adapter: Adapter | None = None) -> set[Path]:
    """Framework and adapter import closure plus explicit proof and data inputs."""
    base = root / "scripts/harness_compat"
    adapters = sorted((base / "adapters").glob("*.py"))
    package = base / "adapters" / harness
    if package.is_dir():
        adapters.extend(sorted(package.rglob("*.py")))
    if not any(path.stem == harness or path.stem.startswith(f"{harness}_") for path in adapters):
        raise FileNotFoundError(f"adapter source absent: {harness}")
    sources = set(base.glob("*.py")) | set(adapters)
    tests = _seed_tests(root, adapter) if adapter is not None else set()
    paths = _closure(root, sources | tests)
    # The lockfile and pytest settings decide what the proofs run on.
    paths |= {root / "uv.lock", root / "pyproject.toml"}
    paths |= set((root / "tests/native").glob("*.py"))
    if _suite_selected(adapter):
        paths |= _suite_processes(root)
    paths |= _tree(root / _PLUGINS / adapter_metadata(adapter, harness).plugin)
    if adapter is not None:
        fixtures = root / "tests/fixtures"
        if fixtures.is_dir():
            for entry in fixtures.glob(f"*{adapter.name}*"):
                paths |= {entry} if entry.is_file() else _tree(entry)
    return {item for item in paths if item.is_file()}


def _normalised(source: str, names: tuple[str, ...]) -> str:
    """Blank the right-hand side of the named assignments so a gate bump leaves no trace."""
    lines = source.splitlines(keepends=True)
    spans = sorted(
        (
            found.value.lineno,
            found.value.col_offset,
            found.value.end_lineno,
            found.value.end_col_offset,
        )
        for found in assignments(source, names)
        if found.value.end_lineno is not None and found.value.end_col_offset is not None
    )
    for start_line, start_col, end_line, end_col in reversed(spans):
        head = lines[start_line - 1][:start_col]
        tail = lines[end_line - 1][end_col:]
        lines[start_line - 1 : end_line] = [f"{head}0{tail}"]
    return "".join(lines)


def plugin_digest(root: Path, harness: str, adapter: Adapter | None = None) -> str:
    """Hash the plugin tree with the verified gate literals normalised away.

    An ordinary bump edits only those literals, so it must not reset the obligation baseline.
    """
    metadata = adapter_metadata(adapter, harness)
    directory = root / _PLUGINS / metadata.plugin
    gates = {
        root / relative: tuple(names) for relative, names in getattr(adapter, "gate_literals", ())
    }
    digest = hashlib.sha256()
    for path in sorted(_tree(directory)):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        names = gates.get(path)
        if names is not None:
            digest.update(_normalised(path.read_text(encoding="utf-8"), names).encode())
        else:
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def adapter_digest(root: Path, harness: str, adapter: Adapter | None = None) -> str:
    """Hash every qualification input by path and content."""
    digest = hashlib.sha256()
    for path in sorted(qualification_inputs(root, harness, adapter)):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
