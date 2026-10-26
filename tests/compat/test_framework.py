"""The suite runner and sandbox: verdicts, diagnostics, isolation and child reaping."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from scripts.harness_compat.contract import Check
from scripts.harness_compat.diagnostics import clip_diagnostic
from scripts.harness_compat.framework import _suite_check, python_path, run_pytest
from scripts.harness_compat.process import (
    SANDBOX_ENV,
    new_token,
    owned_pids,
    run_group,
)
from scripts.harness_compat.sandbox import make_sandbox


def test_run_pytest_maps_skips_and_failures(tmp_path: Path) -> None:
    test = tmp_path / "test_sample.py"
    test.write_text(
        "import pytest\n"
        "def test_pass(): assert True\n"
        "def test_fail(): assert False\n"
        "def test_expected(): pytest.skip('expected')\n"
        "def test_unexpected(): pytest.skip('unexpected')\n"
    )
    check = run_pytest(
        tmp_path,
        "test_sample.py",
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        },
        timeout=30,
    )
    assert isinstance(check, Check) and check.status == "fail"
    assert check.detail.startswith("1 passed, 1 failed, 2 skipped;")


def test_failed_pytest_diagnostics_are_bounded(tmp_path: Path) -> None:
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
    failed = run_pytest(
        tmp_path,
        "test_diagnostics.py",
        env=env,
        timeout=30,
    )
    assert failed.status == "fail"
    assert failed.detail.startswith("1 failed, 1 errored; test_diagnostics.py::test_fail:")
    assert len(failed.detail) <= 600
    assert "[31m" not in failed.detail and "#x1B" not in failed.detail
    captured = ET.Element("testcase", classname="test_diagnostics", name="test_capture")
    ET.SubElement(captured, "failure", message="")
    ET.SubElement(captured, "system-out").text = "ignored " * 200 + "capture tail"
    fallback = _suite_check("test_diagnostics.py", [captured])
    assert fallback.status == "fail"
    assert "test_diagnostics.py::test_capture" in fallback.detail
    assert "capture tail" in fallback.detail


@pytest.mark.parametrize("kind", ["error", "skipped"])
def test_a_skip_or_setup_error_can_never_pass(kind: str) -> None:
    case = ET.Element("testcase", classname="test_suite", name="test_unproven")
    ET.SubElement(case, kind, message="not proven")

    check = _suite_check("tests/conformance/live", [case])

    assert check.status == "error"
    assert "not proven" in check.detail
    assert _suite_check("tests/conformance/live", []).status == "error"


def test_a_failed_case_is_not_hidden_by_a_later_passing_duplicate() -> None:
    failed = ET.Element("testcase", classname="test_suite", name="test_same")
    ET.SubElement(failed, "failure", message="broken")
    passed = ET.Element("testcase", classname="test_suite", name="test_same")

    assert _suite_check("tests/conformance/live", [failed, passed]).status == "fail"


def test_a_non_clean_pytest_exit_cannot_pass_despite_all_passing_cases(tmp_path, monkeypatch):
    def run(argv, **kwargs):
        xml = Path(
            next(arg.removeprefix("--junitxml=") for arg in argv if arg.startswith("--junitxml="))
        )
        xml.write_text('<testsuite><testcase name="test_ok"/></testsuite>')
        return subprocess.CompletedProcess(argv, 1, "session completion failed", "")

    monkeypatch.setattr("scripts.harness_compat.framework.run_group", run)
    check = run_pytest(tmp_path, "test_x.py", env={})
    assert check.status == "error" and "pytest exit 1" in check.detail


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
            assert env["TMUX_TMPDIR"] == str(sb.home)
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
    check = run_pytest(tmp_path, "test_hang.py", env=env, timeout=3)
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


def test_run_group_captures_text_in_requested_cwd(tmp_path: Path) -> None:
    result = run_group(
        [sys.executable, "-c", "import os, sys; print(os.getcwd()); print('é', file=sys.stderr)"],
        cwd=tmp_path,
        env=os.environ,
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == str(tmp_path)
    assert result.stderr.strip() == "é"


def test_run_group_timeout_reaps_its_leader(tmp_path: Path) -> None:
    pidfile = tmp_path / "leader.pid"
    script = (
        "import os, sys, time\nopen(sys.argv[1], 'w').write(str(os.getpid()))\ntime.sleep(60)\n"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        run_group(
            [sys.executable, "-c", script, str(pidfile)], cwd=tmp_path, env=os.environ, timeout=2
        )
    assert _gone(int(pidfile.read_text())), "the timed-out pipe runner left its leader alive"


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


@pytest.mark.tmux
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


def test_pytest_leaves_no_bytecode_or_cache_beside_what_it_runs(tmp_path: Path) -> None:
    (tmp_path / "helper.py").write_text("VALUE = 1\n")
    (tmp_path / "test_clean.py").write_text("import helper\n\ndef test_ok(): assert helper.VALUE\n")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)}

    check = run_pytest(tmp_path, "test_clean.py", env=env, timeout=30)

    assert check.status == "pass"
    assert not list(tmp_path.rglob("__pycache__")) and not (tmp_path / ".pytest_cache").exists()


def test_pytest_refuses_to_run_in_a_git_checkout(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()

    with pytest.raises(ValueError, match="git checkout"):
        run_pytest(tmp_path, "test_x.py", env={}, timeout=5)


def test_the_public_pythonpath_names_the_copy_first_then_its_workspace_sources(
    tmp_path: Path,
) -> None:
    for package in ("zeta", "regie"):
        (tmp_path / "packages" / package / "src").mkdir(parents=True)
    (tmp_path / "packages" / "no_src").mkdir()

    entries = python_path(tmp_path).split(os.pathsep)

    assert entries == [
        str(tmp_path),
        str(tmp_path / "packages/regie/src"),
        str(tmp_path / "packages/zeta/src"),
    ]


def test_the_pythonpath_of_a_plain_tree_is_just_the_tree(tmp_path: Path) -> None:
    assert python_path(tmp_path) == str(tmp_path)
