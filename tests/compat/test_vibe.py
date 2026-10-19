"""Vibe adapter checks, including opt-in real-binary proof nodes."""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.harness_compat.adapters.vibe import ADAPTER
from scripts.harness_compat.adapters.vibe_bump import COMPAT
from scripts.harness_compat.adapters.vibe_store import READER_STORE, store_check
from scripts.harness_compat.contract import Check, InstallFailed, version_key
from scripts.harness_compat.sandbox import make_sandbox
from tests.support.paths import REPO_ROOT
from theater.harness.builtin.plugins.vibe.unified_source import UnifiedVibeSource
from theater.harness.builtin.plugins.vibe.unified_store import load_unified_store


def _proof_path(name: str) -> Path:
    value = os.environ.get(name)
    if value is None:
        pytest.skip(f"{name} is set only by Vibe qualification")
    return Path(value)


def test_real_vibe_store_format() -> None:
    _proof_path("VIBE_COMPAT_BINARY")
    assert int(os.environ["VIBE_COMPAT_UPSTREAM_MINOR"]) <= int(
        os.environ["VIBE_COMPAT_READER_MINOR"]
    )


def test_real_vibe_mock_turn() -> None:
    binary = _proof_path("VIBE_COMPAT_BINARY")
    vibe_home = _proof_path("VIBE_COMPAT_HOME")
    socket = _proof_path("VIBE_COMPAT_TMUX_SOCKET")
    requests = _proof_path("VIBE_COMPAT_REQUESTS")
    command = ["tmux", "-f", "/dev/null", "-S", str(socket)]
    sandbox_env = dict(os.environ)
    try:
        subprocess.run(
            [
                *command,
                "new-session",
                "-d",
                "-x",
                "150",
                "-y",
                "45",
                "-s",
                "vibe-proof",
                "-c",
                str(vibe_home),
                str(binary),
            ],
            env=sandbox_env,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        deadline = time.monotonic() + 45
        screen = ""
        trusted = False
        while time.monotonic() < deadline:
            screen = subprocess.run(
                [*command, "capture-pane", "-p", "-S", "-100"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout
            if "Trust folder" in screen and not trusted:
                subprocess.run([*command, "send-keys", "Enter"], check=True)
                trusted = True
                continue
            if "mock" in screen.lower():
                break
            time.sleep(0.2)
        else:
            pytest.fail(f"Vibe composer did not open: {screen[-1200:]}")
        subprocess.run([*command, "send-keys", "-l", "Reply with MOCK-OK"], check=True)
        subprocess.run([*command, "send-keys", "Enter"], check=True)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            current = list((vibe_home / "logs/session/unified").glob("*/CURRENT"))
            if current and requests.is_file():
                try:
                    view = load_unified_store(current[0])
                    if view and view.snapshot.get("latestTurn", {}).get("status") == "completed":
                        break
                except Exception:
                    pass
            time.sleep(0.2)
        else:
            pytest.fail(f"Vibe turn did not complete: {screen[-1200:]}")
        recorded = [json.loads(line) for line in requests.read_text().splitlines()]
        assert any(
            request["method"] == "POST" and request["path"] == "/v1/chat/completions"
            for request in recorded
        )
        screen = subprocess.run(
            [*command, "capture-pane", "-p", "-S", "-100"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
        assert "MOCK-OK" in screen
    finally:
        subprocess.run([*command, "kill-server"], capture_output=True, timeout=5, check=False)


def test_real_vibe_reader_turn() -> None:
    vibe_home = _proof_path("VIBE_COMPAT_HOME")
    currents = list((vibe_home / "logs/session/unified").glob("*/CURRENT"))
    assert len(currents) == 1
    view = load_unified_store(currents[0])
    assert view is not None
    entries = view.snapshot["history"]["entries"]
    assert {entry.get("role") for entry in entries if entry.get("type") == "message"} >= {
        "user",
        "assistant",
    }
    event = UnifiedVibeSource._turn_boundary(view, previous=None)
    assert event is not None and event.turn_end is True
    assert "MOCK-OK" in event.text


def _tree(root: Path) -> Path:
    source = REPO_ROOT
    for relative in (COMPAT, READER_STORE):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
    return root


def _next_patch(version: str) -> str:
    major, minor, patch = version_key(version)
    return f"{major}.{minor}.{patch + 1}"


def test_vibe_bump_is_patch_bounded_and_idempotent(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    candidate = ADAPTER.verified(root).ceiling
    assert candidate is not None
    latest = _next_patch(candidate)
    new_ceiling = _next_patch(latest)

    changed = ADAPTER.apply_bump(root, [candidate, latest])
    assert changed == [COMPAT]
    assert ADAPTER.verified(root).ceiling == new_ceiling
    assert ADAPTER.apply_bump(root, [candidate, latest]) == []
    ast.parse((root / COMPAT).read_text())


def test_vibe_bump_ignores_composition_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _tree(tmp_path)
    green = ADAPTER.verified(root).ceiling
    assert green is not None
    red = _next_patch(green)
    selection = [green, red]
    calls = 0

    def composition_discovery() -> list[str]:
        nonlocal calls
        calls += 1
        return [green]

    monkeypatch.setattr(ADAPTER, "discover", composition_discovery)
    ADAPTER.apply_bump(root, selection[:1])
    assert calls == 0
    assert ADAPTER.verified(root).ceiling == red
    assert ADAPTER.verified(root).admits(red) is False
    assert ADAPTER.apply_bump(root, [green]) == []


def test_vibe_bump_requires_ceiling_anchor(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    green = ADAPTER.verified(root).ceiling
    assert green is not None
    source = root / COMPAT
    source.write_text(source.read_text().replace("_CEILING = ", "MISSING_CEILING = "))
    before = source.read_text()

    with pytest.raises(ValueError, match="Vibe gate assignment missing: _CEILING"):
        ADAPTER.apply_bump(root, [green])
    assert source.read_text() == before


@pytest.mark.parametrize("bumped", [True, False], ids=["candidate", "watch"])
def test_vibe_qualify_gates_candidate_and_watch_on_consistency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bumped: bool
) -> None:
    root = _tree(tmp_path)
    version = ADAPTER.verified(root).ceiling
    assert version is not None
    if bumped:
        ADAPTER.apply_bump(root, [version])
    failure = Check(ADAPTER.consistency_tests[0], "fail", "broken selector")
    calls: list[tuple[str, ...]] = []

    def run_pytest(_root, nodeids, **_kwargs):
        calls.append(tuple(nodeids))
        return (failure,)

    monkeypatch.setattr("scripts.harness_compat.adapters.vibe.framework.run_pytest", run_pytest)
    monkeypatch.setattr(
        "scripts.harness_compat.adapters.vibe.store_check",
        lambda *_args: pytest.fail("store check ran before consistency gate"),
    )
    with make_sandbox("vibe") as sandbox:
        result = ADAPTER.qualify(root, version, sandbox, sandbox.bin_dir / "vibe")
    assert calls == [ADAPTER.consistency_tests]
    assert result.verdict == "tooling"
    assert result.checks == (failure,)


def test_vibe_qualify_reaches_proofs_after_consistency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _tree(tmp_path)
    version = ADAPTER.verified(root).ceiling
    assert version is not None
    static = Check("vibe_store_format", "pass", "upstream 8 <= reader 8")
    calls: list[tuple[str, ...]] = []

    def run_pytest(_root, nodeids, **_kwargs):
        calls.append(tuple(nodeids))
        return tuple(Check(nodeid, "pass") for nodeid in nodeids)

    monkeypatch.setattr("scripts.harness_compat.adapters.vibe.framework.run_pytest", run_pytest)
    monkeypatch.setattr("scripts.harness_compat.adapters.vibe.store_check", lambda *_args: static)
    monkeypatch.setattr("scripts.harness_compat.adapters.vibe.reader_minor", lambda _root: 8)
    with make_sandbox("vibe") as sandbox:
        result = ADAPTER.qualify(root, version, sandbox, sandbox.bin_dir / "vibe")
    assert calls == [ADAPTER.consistency_tests, ADAPTER.green_tests]
    assert result.verdict == "green"
    assert result.checks[len(ADAPTER.consistency_tests)] == static


def test_vibe_store_format_classification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _tree(tmp_path)
    candidate = ADAPTER.verified(root).ceiling
    assert candidate is not None
    reader = root / READER_STORE
    reader.write_text("STORE_FORMAT_MINOR = 8\n")
    monkeypatch.setattr(
        "scripts.harness_compat.adapters.vibe_store.github.raw_file",
        lambda *_args: "STORE_FORMAT_MINOR = 9\n",
    )
    monkeypatch.setattr(
        "scripts.harness_compat.adapters.vibe.framework.run_pytest",
        lambda _root, nodeids, **_kwargs: tuple(Check(nodeid, "pass") for nodeid in nodeids),
    )
    binary = tmp_path / "vibe"
    monkeypatch.setattr(ADAPTER, "install", lambda *_args: binary)
    check = store_check(root, candidate)
    assert check.name == "vibe_store_format"
    assert check.status == "fail"
    assert check.detail == "reader needs work: upstream store format 9 > ours 8"
    ADAPTER.apply_bump(root, [candidate])
    with make_sandbox("vibe") as sandbox:
        installed = ADAPTER.install(sandbox, candidate)
        result = ADAPTER.qualify(root, candidate, sandbox, installed)
    assert result.verdict == "red"
    assert result.checks[-1] == check


def test_vibe_install_failure_is_inconclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    root = REPO_ROOT
    candidate = ADAPTER.verified(root).ceiling
    assert candidate is not None
    with make_sandbox("vibe") as sandbox:
        monkeypatch.setattr(
            "scripts.harness_compat.adapters.vibe.shutil.which", lambda *_args, **_kw: "/fake/uv"
        )
        monkeypatch.setattr(
            "scripts.harness_compat.adapters.vibe.subprocess.run",
            lambda *_args, **_kw: SimpleNamespace(returncode=1, stderr="wheel unavailable"),
        )
        with pytest.raises(InstallFailed, match="wheel unavailable"):
            ADAPTER.install(sandbox, candidate)


@pytest.mark.parametrize(("version", "python"), [("2.26.1", "3.12"), ("3.0.0", "3.14")])
def test_vibe_install_pins_the_python_the_release_requires(
    monkeypatch: pytest.MonkeyPatch, version: str, python: str
) -> None:
    argv: list[str] = []

    def run(command, *_args, **_kwargs):
        argv.extend(command)
        return SimpleNamespace(returncode=1, stderr="stop here")

    monkeypatch.setattr(
        "scripts.harness_compat.adapters.vibe.shutil.which", lambda *_args, **_kw: "/fake/uv"
    )
    monkeypatch.setattr("scripts.harness_compat.adapters.vibe.subprocess.run", run)
    with make_sandbox("vibe") as sandbox, pytest.raises(InstallFailed):
        ADAPTER.install(sandbox, version)
    assert argv[argv.index("--python") + 1] == python
