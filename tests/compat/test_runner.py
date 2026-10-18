"""The qualification runner: pytest result mapping, selectors, sandbox lifecycle, child reaping."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from scripts.harness_compat.contract import (
    Check,
    Result,
)
from scripts.harness_compat.diagnostics import clip_diagnostic
from scripts.harness_compat.framework import _check_selector, run_pytest
from scripts.harness_compat.process import SANDBOX_ENV, new_token, owned_pids, run_group
from scripts.harness_compat.report import _checks, build_report
from scripts.harness_compat.sandbox import make_sandbox

pytestmark = pytest.mark.usefixtures("isolated_evidence_cache")


def test_run_pytest_maps_skips_and_failures(tmp_path: Path) -> None:
    test = tmp_path / "test_sample.py"
    test.write_text(
        "import pytest\n"
        "def test_pass(): assert True\n"
        "def test_fail(): assert False\n"
        "def test_expected(): pytest.skip('expected')\n"
        "def test_unexpected(): pytest.skip('unexpected')\n"
    )
    nodes = tuple(
        f"test_sample.py::test_{name}" for name in ("pass", "fail", "expected", "unexpected")
    )
    checks = run_pytest(
        tmp_path,
        nodes,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        },
        expected_skips={nodes[2]: "known"},
        timeout=30,
    )
    assert [check.status for check in checks] == ["pass", "fail", "skip", "error"]
    assert all(isinstance(check, Check) for check in checks)


def test_failed_pytest_diagnostics_and_report_rows_are_bounded(tmp_path: Path) -> None:
    (tmp_path / "test_diagnostics.py").write_text(
        "import pytest\n"
        "@pytest.fixture\n"
        "def broken(): raise RuntimeError('setup crash')\n"
        "def test_fail():\n"
        "    print('captured output')\n"
        "    assert False, '\\x1b[31m' + 'X' * 1200 + '\\x1b[0m'\n"
        "def test_setup(broken): pass\n"
    )
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    failed, errored = run_pytest(
        tmp_path,
        ("test_diagnostics.py", "test_diagnostics.py::test_setup"),
        env=env,
        expected_skips={},
        timeout=30,
    )
    assert failed.status == "fail"
    assert failed.detail.startswith("1 failed, 1 errored; test_diagnostics.py::test_fail:")
    assert len(failed.detail) <= 600
    assert "[31m" not in failed.detail and "#x1B" not in failed.detail
    assert errored.status == "error"
    assert "test_diagnostics.py::test_setup:" in errored.detail
    assert "RuntimeError: setup crash" in errored.detail
    captured = ET.Element("testcase")
    ET.SubElement(captured, "failure", message="")
    ET.SubElement(captured, "system-out").text = "ignored " * 200 + "capture tail"
    fallback = _check_selector(
        "test_diagnostics.py::test_capture", {"test_diagnostics.py::test_capture": captured}, {}
    )
    assert fallback.status == "fail"
    assert "test_diagnostics.py::test_capture" in fallback.detail
    assert "capture tail" in fallback.detail

    rows = (
        Result("pi", "0.99.2", "red", (failed,)),
        Result("pi", "0.99.3", "tooling", (Check("mock route", "fail", "Y" * 4000),)),
    )
    report = build_report("pi", rows, run_url="local")
    issue_rows = [line for line in report.issue_body.splitlines() if line.startswith("| 0.99.")]
    assert len(issue_rows) == 2
    assert all(len(line) <= 1700 for line in issue_rows)
    assert "test_diagnostics.py::test_fail" in issue_rows[0]
    assert "[31m" not in report.issue_body
    inconclusive = Result("pi", "0.99.4", "inconclusive", (Check("infra", "error", "Z" * 4000),))
    assert len(_checks(inconclusive)) <= 1600


def test_run_pytest_aggregates_file_directory_class_and_parameter_selectors(tmp_path: Path) -> None:
    proofs = tmp_path / "proofs"
    proofs.mkdir()
    (proofs / "test_selectors.py").write_text(
        "import pytest\n"
        "def test_top(): assert True\n"
        "class TestBundleIndexAndAllowlist:\n"
        "    def test_first(self): assert True\n"
        "    def test_second(self): assert True\n"
        "@pytest.mark.parametrize('value', [1, 2])\n"
        "def test_param(value): assert value > 0\n"
    )
    (tmp_path / "test_skip.py").write_text(
        "import pytest\n"
        "@pytest.mark.parametrize('value', [1, 2])\n"
        "def test_skip(value): pytest.skip('unavailable')\n"
    )
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    file = "proofs/test_selectors.py"
    selectors = (
        "proofs",
        file,
        f"{file}::TestBundleIndexAndAllowlist",
        f"{file}::test_param",
        f"{file}::test_param[1]",
    )
    checks = run_pytest(tmp_path, selectors, env=env, expected_skips={}, timeout=30)
    assert [check.status for check in checks] == ["pass"] * len(selectors)
    assert [check.detail for check in checks] == [
        "5 passed",
        "5 passed",
        "2 passed",
        "2 passed",
        "1 passed",
    ]
    unknown = run_pytest(
        tmp_path, (f"{file}::test_missing",), env=env, expected_skips={}, timeout=30
    )
    assert unknown[0].status == "error"
    unexpected = run_pytest(tmp_path, ("test_skip.py",), env=env, expected_skips={}, timeout=30)
    assert unexpected[0].status == "error"
    assert "2 skipped" in unexpected[0].detail
    expected = run_pytest(
        tmp_path,
        ("test_skip.py::test_skip",),
        env=env,
        expected_skips={"test_skip.py::test_skip": "known"},
        timeout=30,
    )
    assert expected[0].status == "skip"
    assert expected[0].detail == "2 skipped"


def test_sandbox_keeps_unix_socket_paths_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with make_sandbox("codex") as sb:
        if Path("/tmp").is_dir():
            assert sb.root.is_relative_to(Path("/tmp").resolve())
        assert sb.root == sb.root.resolve()
        assert sb.env["TMPDIR"] == str(sb.root / "t")
        socket = (
            sb.evidence_dir
            / "0.162.100"
            / sb.home.name
            / ".codex/sockets"
            / f"app-server-{'0' * 32}.sock"
        )
        assert len(os.fsencode(socket)) < 100
        assert len(os.fsencode(sb.tmux_socket)) < 100
        for env in (sb.env, sb.offline_env):
            assert "ANTHROPIC_API_KEY" not in env
            assert "OPENAI_API_KEY" not in env

    long_root = tmp_path / ("x" * 120)

    def long_temp(*args: object, **kwargs: object) -> str:
        long_root.mkdir()
        return str(long_root)

    monkeypatch.setattr("scripts.harness_compat.sandbox.tempfile.mkdtemp", long_temp)
    with (
        pytest.raises(RuntimeError, match="tmux AF_UNIX socket path is too long"),
        make_sandbox("codex"),
    ):
        pass
    assert not long_root.exists()


def test_clip_keeps_the_named_cause_ahead_of_a_long_stack_tail() -> None:
    stack = " at frame (file.js:1:1)" * 40
    text = (
        "AssertionError: stderr: import { A } from 'pkg'; ^^^ SyntaxError: The requested "
        f"module 'pkg' does not provide an export named 'A' at ModuleJob.run{stack}"
    )
    clipped = clip_diagnostic(text, 300, tail=True)
    assert len(clipped) <= 300
    assert clipped.startswith("SyntaxError: The requested module 'pkg' does not provide")
    assert clipped.endswith("(file.js:1:1)")
    assert clip_diagnostic("plain failure " * 80, 100, tail=True).startswith("…")


def _gone(pid: int) -> bool:
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    os.kill(pid, 9)
    return False


def test_run_pytest_timeout_reaps_detached_children_it_started(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    (tmp_path / "test_hang.py").write_text(
        "import os, subprocess, time\n\n\n"
        "def test_hang():\n"
        "    child = subprocess.Popen(['sleep', '60'], start_new_session=True)\n"
        "    open(os.environ['CHILD_PIDFILE'], 'w').write(str(child.pid))\n"
        "    time.sleep(60)\n"
    )
    env = {**os.environ, "CHILD_PIDFILE": str(pidfile)}
    (check,) = run_pytest(tmp_path, ["test_hang.py"], env=env, expected_skips={}, timeout=3)
    assert check.status == "error"
    assert _gone(int(pidfile.read_text())), "the sleeping child outlived the timed-out pytest run"


def test_run_group_reaps_a_detached_child_after_the_leader_exited(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    script = (
        "import subprocess, sys\n"
        "child = subprocess.Popen(['sleep', '60'], start_new_session=True)\n"
        "open(sys.argv[1], 'w').write(str(child.pid))\n"
    )
    argv = [sys.executable, "-c", script, str(pidfile)]
    assert run_group(argv, cwd=tmp_path, env=os.environ, timeout=30).returncode == 0
    assert _gone(int(pidfile.read_text())), "the detached child outlived its finished leader"


def test_ownership_needs_an_exact_environment_entry() -> None:
    token = new_token()
    entry = f"{SANDBOX_ENV}={token}"
    sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
    clean = {"PATH": os.environ["PATH"]}
    owned = subprocess.Popen(sleeper, env={**clean, SANDBOX_ENV: token})
    argument = subprocess.Popen([*sleeper, entry], env=clean)
    embedded = subprocess.Popen(sleeper, env={**clean, "NOTE": f"x {entry}"})
    try:
        found = owned_pids(SANDBOX_ENV, token)
        assert owned.pid in found
        assert argument.pid not in found and embedded.pid not in found
    finally:
        for process in (owned, argument, embedded):
            process.kill()
            process.wait()


def test_sandbox_removes_read_only_trees() -> None:
    with make_sandbox("vibe") as sb:
        locked = sb.config_dir / "plugins" / "pkg"
        locked.mkdir(parents=True)
        (locked / "file.txt").write_text("x")
        (locked / "file.txt").chmod(0o400)
        locked.chmod(0o500)
        hidden = sb.config_dir / "hidden"
        hidden.mkdir()
        (hidden / "file.txt").write_text("x")
        hidden.chmod(0)
        root = sb.root
    assert not root.exists()


def _tmux_alive(socket: Path) -> bool:
    done = subprocess.run(["tmux", "-S", str(socket), "ls"], capture_output=True, check=False)
    return done.returncode == 0


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_sandbox_teardown_stops_what_it_owns_and_nothing_else() -> None:
    sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
    with make_sandbox("vibe") as sb:
        sockets = [sb.tmux_socket, sb.root / "second.sock"]
        for socket in sockets:
            subprocess.run(
                ["tmux", "-S", str(socket), "new-session", "-d", "sleep 60"],
                capture_output=True,
                check=True,
            )
            assert _tmux_alive(socket)
        owned = subprocess.Popen(sleeper, env=sb.env, start_new_session=True)
        bystander = subprocess.Popen([*sleeper, str(sb.root)], start_new_session=True)
    try:
        assert not any(_tmux_alive(socket) for socket in sockets)
        assert owned.wait(timeout=10) != 0  # detached, but it carries the sandbox token
        assert bystander.poll() is None  # naming the sandbox path is not ownership
    finally:
        bystander.kill()
        bystander.wait()
