"""Guard the paths and line counts published by compatibility bumps."""

from __future__ import annotations

import difflib
import shutil
from pathlib import Path

import pytest

from scripts.harness_compat.adapters import (
    claude,
    codex,
    opencode,
    opencode_bump,
    pi,
    pi_bump,
    vibe,
    vibe_bump,
)
from scripts.harness_compat.contract import version_key

_SOURCE = Path(__file__).parents[1]
_ADAPTERS = {
    "claude": claude.ADAPTER,
    "codex": codex.ADAPTER,
    "opencode": opencode.ADAPTER,
    "pi": pi.ADAPTER,
    "vibe": vibe.ADAPTER,
}
_INPUTS = {
    "claude": (claude._PROBE_SOURCE,),
    "codex": (codex._PLAN, codex._INDEX),
    "opencode": (opencode_bump.RUNTIME_PLAN,),
    "pi": (pi_bump._CONSTANTS,),
    "vibe": (vibe_bump.COMPAT,),
}


def _snapshot(root: Path) -> dict[Path, bytes]:
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _changed_lines(before: bytes, after: bytes) -> int:
    diff = difflib.unified_diff(before.decode().splitlines(), after.decode().splitlines())
    return sum(line.startswith(("+", "-")) and not line.startswith(("+++", "---")) for line in diff)


def _measure_bump(name: str, root: Path) -> tuple[set[Path], int, set[Path]]:
    for relative in _INPUTS[name]:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_SOURCE / relative, target)
    # Snapshot before staging Codex input so only publishable changes count.
    before = _snapshot(root)
    adapter = _ADAPTERS[name]
    gate = adapter.verified(root)
    declared = set(_INPUTS[name]) if name != "claude" else set()

    if name == "codex":
        highest = max(gate.versions, key=version_key)
        major, minor, patch = version_key(highest)
        version = f"{major}.{minor}.{patch + 1}"
        available = [
            path
            for path in (_SOURCE / codex._FIXTURES).iterdir()
            if path.name in gate.versions and path.is_dir() and codex._complete_bundle(path)
        ]
        source_bundle = max(available, key=lambda path: version_key(path.name))
        staged = codex._FIXTURES / "candidates" / version
        shutil.copytree(source_bundle, root / staged)
        published = codex._FIXTURES / version
        declared.update(
            published / path.relative_to(root / staged)
            for path in (root / staged).rglob("*")
            if path.is_file()
        )
    elif name == "claude":
        assert gate.floor is not None
        version = gate.floor
    else:
        assert gate.ceiling is not None
        version = gate.ceiling

    reported = adapter.apply_bump(root, [version])
    after = _snapshot(root)
    changed = {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}
    assert len(reported) == len(set(reported))
    assert set(reported) == changed
    assert changed <= declared, f"{name}: undeclared paths {sorted(changed - declared)}"
    assert changed == declared, (
        f"{name}: declared paths left unchanged {sorted(declared - changed)}"
    )
    assert not any(path.suffix == ".md" or path.name.startswith("test_") for path in changed)
    assert not any(path.parts[:3] == ("tests", "fixtures", "harness_compat") for path in changed)

    lines = sum(_changed_lines(before.get(path, b""), after.get(path, b"")) for path in changed)
    if name in {"opencode", "pi", "vibe"}:
        assert lines <= 4, f"{name}: {lines} changed diff lines"
    assert adapter.apply_bump(root, [version]) == []
    assert _snapshot(root) == after
    return changed, lines, declared


@pytest.mark.parametrize("name", ("claude", "codex", "opencode", "pi", "vibe"))
def test_apply_bump_publishes_only_declared_small_changes(name: str, tmp_path: Path) -> None:
    _measure_bump(name, tmp_path)
