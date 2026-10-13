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
from scripts.harness_compat.adapters.vibe_bump import COMPAT, COMPAT_TEST, DOC
from scripts.harness_compat.adapters.vibe_store import READER_STORE, store_check
from scripts.harness_compat.contract import Check, InstallFailed
from scripts.harness_compat.sandbox import make_sandbox
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


def test_vibe_gate_and_bump_are_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(__file__).resolve().parents[1]
    for relative in (COMPAT, COMPAT_TEST, READER_STORE, DOC):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, target)
    monkeypatch.setattr(
        "scripts.harness_compat.adapters.vibe_bump.store_check",
        lambda _root, _version: Check("vibe_store_format", "pass"),
    )
    assert ADAPTER.verified(tmp_path).ceiling == "2.27.0"
    changed = ADAPTER.apply_bump(tmp_path, ["2.27.0"])
    assert changed == [COMPAT, COMPAT_TEST, DOC]
    assert ADAPTER.verified(tmp_path).ceiling == "2.27.1"
    assert ADAPTER.apply_bump(tmp_path, ["2.27.0"]) == []
    assert "<2.27.1" in (tmp_path / COMPAT_TEST).read_text()
    assert "Vibe compatibility range: >=2.24.0,<2.27.1." in (tmp_path / DOC).read_text()
    for relative in (COMPAT, COMPAT_TEST):
        source = (tmp_path / relative).read_text()
        ast.parse(source)
        assert "2.27.0" not in source


def test_vibe_store_format_classification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reader = tmp_path / READER_STORE
    reader.parent.mkdir(parents=True)
    reader.write_text("STORE_FORMAT_MINOR = 8\n")
    monkeypatch.setattr(
        "scripts.harness_compat.adapters.vibe_store.github.raw_file",
        lambda *_args: "STORE_FORMAT_MINOR = 9\n",
    )
    check = store_check(tmp_path, "2.27.0")
    assert check.status == "fail"
    assert check.detail == "reader needs work: upstream store format 9 > ours 8"
    with make_sandbox("vibe") as sandbox:
        result = ADAPTER.qualify(tmp_path, "2.27.0", sandbox, sandbox.bin_dir / "vibe")
    assert result.verdict == "red"
    assert result.checks == (check,)


def test_vibe_install_failure_is_inconclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    with make_sandbox("vibe") as sandbox:
        monkeypatch.setattr(
            "scripts.harness_compat.adapters.vibe.shutil.which", lambda *_args, **_kw: "/fake/uv"
        )
        monkeypatch.setattr(
            "scripts.harness_compat.adapters.vibe.subprocess.run",
            lambda *_args, **_kw: SimpleNamespace(returncode=1, stderr="wheel unavailable"),
        )
        with pytest.raises(InstallFailed, match="wheel unavailable"):
            ADAPTER.install(sandbox, "2.27.0")
