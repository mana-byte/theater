"""Cleanup distinguishes vanished children from incomplete deletion and shutdown."""

from __future__ import annotations

import contextlib
import errno
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness_compat import cleanup, process
from harness_compat.diagnostics import exception_detail
from harness_compat.sandbox import make_sandbox


def test_a_child_deleted_during_traversal_does_not_fail_cleanup(tmp_path, monkeypatch):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "vanished").touch()
    unlink = os.unlink

    def disappear(path, *args, **kwargs):
        if Path(path).name == "vanished":
            unlink(path, *args, **kwargs)
        unlink(path, *args, **kwargs)

    monkeypatch.setattr(cleanup.shutil.os, "unlink", disappear)
    cleanup.remove_tree(root)
    assert not root.exists()


def test_a_real_deletion_failure_reports_the_operation_and_remaining_root(tmp_path, monkeypatch):
    child = tmp_path / "blocked"
    child.touch()

    def refuse(path, *, onexc):
        onexc(os.unlink, str(child), PermissionError(errno.EACCES, "denied", str(child)))

    with monkeypatch.context() as patch:
        patch.setattr(cleanup.shutil, "rmtree", refuse)
        with pytest.raises(cleanup.CleanupFailed) as caught:
            cleanup.remove_tree(tmp_path)
    message = str(caught.value)
    assert "unlink" in message and str(child) in message
    assert "PermissionError" in message and "Errno 13" in message
    assert "remaining=True" in message


def test_shutdown_waits_after_sigkill(monkeypatch):
    alive = True
    killed = False

    def send(pid, signum):
        nonlocal killed
        if signum == signal.SIGKILL:
            killed = True

    def wait(seconds):
        nonlocal alive
        if killed:
            alive = False

    monkeypatch.setattr(process, "_alive", lambda pid: alive)
    monkeypatch.setattr(process, "_ps", lambda *args: "")
    monkeypatch.setattr(process.os, "kill", send)
    monkeypatch.setattr(process.time, "sleep", wait)
    process.kill_all([4242], grace=0)
    assert not alive, "shutdown returned while the killed process was still running"


def test_shutdown_discovers_children_born_after_the_first_sweep(monkeypatch):
    live = {4242}

    def kill(pids, **kwargs):
        live.difference_update(pids)
        if 4242 in pids:
            live.add(4343)

    monkeypatch.setattr(process, "owned_pids", lambda *args: live.copy())
    monkeypatch.setattr(process, "ancestry", lambda *args: {os.getpid()})
    monkeypatch.setattr(process, "_alive", lambda pid: pid in live)
    monkeypatch.setattr(process, "_ps", lambda *args: "")
    monkeypatch.setattr(process, "kill_all", kill)
    process.stop_owned(process.SANDBOX_ENV, "owned")
    assert not live, "a late child escaped the ownership sweep"


def test_an_undeleted_root_fails_even_without_an_error_callback(tmp_path, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(cleanup.shutil, "rmtree", lambda *args, **kwargs: None)
        with pytest.raises(cleanup.CleanupFailed, match="remaining=True"):
            cleanup.remove_tree(tmp_path)


def test_shutdown_fails_if_sigkill_does_not_stop_the_process(monkeypatch):
    monkeypatch.setattr(process, "_GRACE_SECONDS", 0)
    monkeypatch.setattr(process, "_alive", lambda pid: True)
    monkeypatch.setattr(process, "_ps", lambda *args: "")
    monkeypatch.setattr(process.os, "kill", lambda *args: None)
    with pytest.raises(cleanup.CleanupFailed, match=r"4242.*survived SIGKILL"):
        process.kill_all([4242], grace=0)


def test_zombies_do_not_block_cleanup(monkeypatch):
    monkeypatch.setattr(process, "_alive", lambda pid: True)
    monkeypatch.setattr(process, "_ps", lambda *args: "4242 Z+\n")
    monkeypatch.setattr(process.os, "kill", lambda *args: pytest.fail("signalled a zombie"))
    process.kill_all([4242], grace=0)


def test_repeated_ownership_sweeps_have_a_deadline(monkeypatch):
    ticks = iter(range(10))
    monkeypatch.setattr(process.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(process, "_GRACE_SECONDS", 1)
    monkeypatch.setattr(process, "owned_pids", lambda *args: {4242})
    monkeypatch.setattr(process, "ancestry", lambda *args: {os.getpid()})
    monkeypatch.setattr(process, "_alive", lambda pid: True)
    monkeypatch.setattr(process, "_ps", lambda *args: "")
    monkeypatch.setattr(process, "kill_all", lambda *args, **kwargs: None)
    with pytest.raises(cleanup.CleanupFailed, match=r"owned PIDs.*4242.*remain"):
        process.stop_owned(process.SANDBOX_ENV, "owned")


def test_sandbox_keeps_data_if_process_shutdown_fails(theater_home, monkeypatch):
    root = theater_home / "sandbox"
    monkeypatch.setattr("harness_compat.sandbox._new_temp_dir", lambda: root)

    def failed(*args):
        raise cleanup.CleanupFailed("processes still running")

    monkeypatch.setattr("harness_compat.sandbox.stop_sandbox_processes", failed)
    try:
        with (
            pytest.raises(cleanup.CleanupFailed, match="processes still running"),
            make_sandbox("test"),
        ):
            pass
        assert root.exists(), "deleted files while their owning processes were still running"
    finally:
        cleanup.remove_tree(root)


def test_a_real_detached_child_born_during_shutdown_is_stopped(tmp_path):
    token = process.new_token()
    pidfile = tmp_path / "late.pid"
    script = (
        "import os, signal, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "def stop(*args):\n"
        "    child = subprocess.Popen(['sleep', '60'], start_new_session=True, "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "    Path(sys.argv[1]).write_text(str(child.pid))\n"
        "    os._exit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", script, str(pidfile)],
        env={**os.environ, process.SANDBOX_ENV: token},
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert parent.stdout is not None and parent.stdout.readline().strip() == "ready"
        threading.Thread(target=parent.wait, daemon=True).start()
        process.stop_owned(process.SANDBOX_ENV, token)
        child_pid = int(pidfile.read_text())
        assert not process._live_pids({parent.pid, child_pid})
    finally:
        for pid in process.owned_pids(process.SANDBOX_ENV, token):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        parent.wait(timeout=5)
        if parent.stdout is not None:
            parent.stdout.close()


def test_cleanup_diagnostics_keep_the_preceding_exception():
    error = cleanup.CleanupFailed("deletion failure")
    error.__context__ = ValueError("original failure")
    detail = exception_detail(error)
    assert "deletion failure" in detail and "ValueError: original failure" in detail


def test_leader_shutdown_is_bounded_and_still_stops_its_descendants(monkeypatch):
    waits = []
    stopped = []

    def wait(*, timeout):
        waits.append(timeout)
        raise subprocess.TimeoutExpired("leader", timeout)

    monkeypatch.setattr(process, "_signal_group", lambda *args: None)
    monkeypatch.setattr(process, "kill_all", stopped.append)
    leader = SimpleNamespace(pid=4242, wait=wait)
    with pytest.raises(cleanup.CleanupFailed, match="leader PID 4242 survived SIGKILL"):
        process.terminate_group(leader, {4343})
    assert waits == [process._GRACE_SECONDS, process._GRACE_SECONDS]
    assert stopped == [{4343}]
