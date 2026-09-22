"""Candidate subprocess containment for the RC10 implementation waves."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from rc10_support.isolation import (
    MAX_UNIX_SOCKET_PATH_BYTES,
    CleanupBlocked,
    create_candidate_sandbox,
)


@pytest.fixture
def sandbox():
    candidate = create_candidate_sandbox()
    try:
        yield candidate
    finally:
        if candidate.paths.root.exists():
            candidate.cleanup()


def test_candidate_environment_is_sanitized_without_mutating_its_base(sandbox):
    base = {
        "KEEP": "yes",
        "PYTHONPATH": "/control/source",
        "THEATER_HOME": "/control/home",
        "THEATER_ID": "control-participant",
        "THEATER_PLUGIN_CREDENTIAL_PATH": "/control/credential",
        "THEATER_RUNTIME_TOKEN": "control-token",
        "TMUX": "/private/tmp/tmux-501/default,123,0",
        "TMUX_PANE": "%99",
        "TMUX_TMPDIR": "/private/tmp",
        "CLAUDE_CODE_MESSAGING_SOCKET": "/control/claude.sock",
        "CLAUDE_CODE_MESSAGING_TOKEN": "control-claude-token",
        "OPENCODE_SERVER_PASSWORD": "control-password",
        "VIBE_MCP_SERVERS": "control-mcp",
    }
    original = dict(base)

    environment = sandbox.environment(base, extra_environment={"FOCUSED_TEST": "1"})

    assert base == original
    assert environment["KEEP"] == "yes"
    assert environment["FOCUSED_TEST"] == "1"
    assert environment["THEATER_HOME"] == str(sandbox.paths.theater_home)
    assert environment["TMUX_TMPDIR"] == str(sandbox.paths.tmux_root)
    for name in original:
        if name not in {"KEEP", "THEATER_HOME", "TMUX_TMPDIR"}:
            assert name not in environment


def test_candidate_paths_are_unique_short_and_under_tmp():
    first = create_candidate_sandbox()
    second = create_candidate_sandbox()
    try:
        assert first.paths.root != second.paths.root
        for candidate in (first, second):
            assert str(candidate.paths.root).startswith("/tmp/")
            assert candidate.paths.theater_home.parent == candidate.paths.root
            assert candidate.paths.tmux_root.parent == candidate.paths.root
            for path in candidate.paths.socket_paths():
                assert len(os.fsencode(path.resolve(strict=False))) <= MAX_UNIX_SOCKET_PATH_BYTES
    finally:
        first.cleanup()
        second.cleanup()


def test_cleanup_preserves_an_unrelated_live_socket_after_an_owned_child_exits(sandbox, tmp_path):
    process = sandbox.start(
        [sys.executable, "-c", "pass"],
        base_environment={"PATH": os.environ["PATH"]},
        cwd=tmp_path,
    )
    assert process.wait(timeout=5) == 0
    socket_path = sandbox.paths.tmux_root / "tmux-501" / "default"
    socket_path.parent.mkdir()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    server.listen()
    try:
        with pytest.raises(CleanupBlocked, match="socket"):
            sandbox.cleanup()
        assert sandbox.paths.root.exists()
        assert socket_path.exists()
    finally:
        server.close()
        socket_path.unlink()
    sandbox.cleanup()
    assert not sandbox.paths.root.exists()


def test_cleanup_waits_for_a_test_owned_child_to_stop(sandbox, tmp_path):
    process = sandbox.start(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        base_environment={"PATH": os.environ["PATH"]},
        cwd=tmp_path,
    )
    try:
        with pytest.raises(CleanupBlocked, match="still running"):
            sandbox.cleanup()
        assert sandbox.paths.root.exists()
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_runner_sanitizes_only_its_explicit_child(tmp_path):
    runner = Path(__file__).parent / "rc10_support" / "run_candidate.py"
    keys = ["THEATER_HOME", "THEATER_ID", "TMUX", "TMUX_PANE", "TMUX_TMPDIR"]
    script = f"import json, os; print(json.dumps({{key: os.environ.get(key) for key in {keys!r}}}))"
    base = {
        "PATH": os.environ["PATH"],
        "THEATER_HOME": "/control/home",
        "THEATER_ID": "control-participant",
        "TMUX": "/private/tmp/tmux-501/default,123,0",
        "TMUX_PANE": "%99",
        "THEATER_RUNTIME_TOKEN": "control-token",
    }
    original = dict(base)
    parent_environment = dict(os.environ)

    result = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--candidate-executable",
            sys.executable,
            "--candidate-cwd",
            str(tmp_path),
            "--",
            "-c",
            script,
        ],
        check=False,
        capture_output=True,
        env=base,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert base == original
    assert dict(os.environ) == parent_environment
    environment = json.loads(result.stdout)
    assert environment["THEATER_HOME"].startswith("/tmp/rc10-")
    assert environment["TMUX_TMPDIR"].startswith("/tmp/rc10-")
    assert environment["THEATER_ID"] is None
    assert environment["TMUX"] is None
    assert environment["TMUX_PANE"] is None


def test_runner_rejects_a_default_cli_target(tmp_path):
    runner = Path(__file__).parent / "rc10_support" / "run_candidate.py"

    result = subprocess.run(
        [sys.executable, str(runner), "--candidate-cwd", str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert (
        "one of the arguments --candidate-executable --candidate-module is required"
        in result.stderr
    )
