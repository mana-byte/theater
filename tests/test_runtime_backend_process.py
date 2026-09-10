"""Focused tests for detached backend process ownership.

Real processes (python subprocesses), real signals, real identity facts — the
launch path, session detachment, verified pid identity, graceful terminate,
kill fallback, and the fail-closed mismatch rule.
"""

from __future__ import annotations

import asyncio
import os
import signal
import stat
import sys
from pathlib import Path

import pytest

from theater import paths
from theater.daemon.harness_runtime.backend import (
    BackendProcessIdentity,
    DetachedBackendProcess,
    capture_process_identity,
    launch_detached_backend,
    pid_alive,
    process_start_token,
    verify_process_identity,
)
from theater.daemon.harness_runtime.errors import (
    BackendIdentityMismatch,
    BackendProcessError,
)
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.runtime import RuntimePlan

SLEEP_SNIPPET = "import time; time.sleep(300)"
IGNORE_SIGTERM_SNIPPET = (
    "import signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "print('ready', flush=True)\n"
    "time.sleep(300)\n"
)


def _plan(argv: list[str], *, endpoint: str = "unix:///tmp/thtr-nonexistent.sock") -> RuntimePlan:
    return RuntimePlan(backend=LaunchPlan(argv=argv), endpoint=endpoint)


def _python(argv: list[str]) -> list[str]:
    return [sys.executable, *argv]


async def _launch(argv: list[str], participant: str, cwd: Path) -> DetachedBackendProcess:
    return await launch_detached_backend(_plan(argv), participant_id=participant, cwd=cwd)


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    directory = tmp_path / "work"
    directory.mkdir()
    return directory


async def test_launch_writes_plan_files_env_and_private_logs(workdir: Path) -> None:
    secret_path = paths.participant_dir("p-files") / "launch" / "secret.token"
    config_path = paths.participant_dir("p-files") / "launch" / "config.json"
    marker_snippet = (
        "import os, time; print(os.environ['THEATER_MARKER'], flush=True); time.sleep(300)"
    )
    plan = RuntimePlan(
        backend=LaunchPlan(
            argv=_python(["-c", marker_snippet]),
            env={"THEATER_MARKER": "engine-42"},
            files={config_path: '{"public": true}'},
            private_files={secret_path: "s3cret"},
        ),
        endpoint="unix:///tmp/thtr-p-files.sock",
    )
    backend = await launch_detached_backend(plan, participant_id="p-files", cwd=workdir)
    try:
        assert config_path.read_text() == '{"public": true}'
        assert secret_path.read_text() == "s3cret"
        assert stat.S_IMODE(secret_path.stat().st_mode) & 0o777 == 0o600
        output = await asyncio.to_thread(_wait_for_line, backend.stdout_path, b"engine-42")
        assert b"engine-42" in output, "the backend env must reach the detached process"
    finally:
        await backend.terminate()


def _wait_for_line(path: Path, marker: bytes, attempts: int = 200) -> bytes:
    for _ in range(attempts):
        try:
            if marker in path.read_bytes():
                return path.read_bytes()
        except OSError:
            pass
        import time

        time.sleep(0.05)
    return b""


async def test_backend_runs_in_its_own_session_not_ours(workdir: Path) -> None:
    backend = await _launch(_python(["-c", SLEEP_SNIPPET]), "p-session", workdir)
    try:
        assert backend.pid > 0
        # start_new_session makes the backend its own session leader.
        assert os.getsid(backend.pid) == backend.pid
        assert os.getsid(backend.pid) != os.getsid(os.getpid())
        assert backend.alive()
    finally:
        await backend.terminate()
    assert not pid_alive(backend.pid) or not backend.alive()


async def test_identity_roundtrip_and_pid_gone_fails_closed(workdir: Path) -> None:
    backend = await _launch(_python(["-c", SLEEP_SNIPPET]), "p-identity", workdir)
    pid = backend.pid
    try:
        verify_process_identity(backend.identity)  # alive, matches: passes
        os.kill(pid, signal.SIGKILL)
        await backend.wait()
        with pytest.raises(BackendIdentityMismatch, match="gone"):
            verify_process_identity(backend.identity)
    finally:
        if pid_alive(pid):
            os.kill(pid, signal.SIGKILL)


async def test_start_token_mismatch_is_a_mismatch_without_signaling(workdir: Path) -> None:
    backend = await _launch(_python(["-c", SLEEP_SNIPPET]), "p-token", workdir)
    try:
        wrong_token = BackendProcessIdentity(
            pid=backend.pid, comm=backend.identity.comm, start_token="999999999999"
        )
        with pytest.raises(BackendIdentityMismatch, match="start token"):
            verify_process_identity(wrong_token)
        assert backend.alive(), "a mismatch must never produce a signal"
        verify_process_identity(backend.identity)  # the real identity still passes
    finally:
        await backend.terminate()


async def test_terminate_with_mismatched_identity_never_signals(workdir: Path) -> None:
    backend = await _launch(_python(["-c", IGNORE_SIGTERM_SNIPPET]), "p-mismatch", workdir)
    try:
        impostor = DetachedBackendProcess.__new__(DetachedBackendProcess)
        impostor._process = backend._process
        impostor._identity = BackendProcessIdentity(
            pid=backend.pid, comm="not-our-backend", start_token=backend.identity.start_token
        )
        impostor._endpoint = backend.endpoint
        impostor._stdout_path = backend.stdout_path
        impostor._stderr_path = backend.stderr_path
        with pytest.raises(BackendIdentityMismatch):
            await impostor.terminate(grace=0.2)
        assert backend.alive(), "mismatch means no signal was sent"
    finally:
        await backend.terminate(grace=0.2)


async def test_graceful_terminate_uses_sigterm_then_reaps(workdir: Path) -> None:
    backend = await _launch(_python(["-c", SLEEP_SNIPPET]), "p-term", workdir)
    assert backend.alive()
    await asyncio.wait_for(backend.terminate(grace=5.0), timeout=15.0)
    assert not backend.alive()
    assert not pid_alive(backend.pid)


async def test_kill_fallback_after_grace(workdir: Path) -> None:
    backend = await _launch(_python(["-c", IGNORE_SIGTERM_SNIPPET]), "p-kill", workdir)
    output = await asyncio.to_thread(_wait_for_line, backend.stdout_path, b"ready")
    assert b"ready" in output, "the SIGTERM-ignoring backend got far enough to run"
    await asyncio.wait_for(backend.terminate(grace=0.5), timeout=20.0)
    assert not backend.alive()
    assert not pid_alive(backend.pid)


async def test_unkillable_process_reports_failure(workdir: Path) -> None:
    # A process holding a stuck state: SIGKILL ignored is not possible for
    # SIGKILL, so instead simulate the kill not landing by pointing the
    # identity at a different live process we own and expect a mismatch.
    blocker = await _launch(_python(["-c", SLEEP_SNIPPET]), "p-blocker", workdir)
    backend = await _launch(_python(["-c", SLEEP_SNIPPET]), "p-blocker2", workdir)
    try:
        # Craft a handle whose process disagrees with its recorded identity.
        mismatched = DetachedBackendProcess.__new__(DetachedBackendProcess)
        mismatched._process = backend._process
        mismatched._identity = BackendProcessIdentity(
            pid=blocker.pid, comm=backend.identity.comm, start_token=backend.identity.start_token
        )
        mismatched._endpoint = backend.endpoint
        mismatched._stdout_path = backend.stdout_path
        mismatched._stderr_path = backend.stderr_path
        with pytest.raises((BackendIdentityMismatch, BackendProcessError)):
            await mismatched.terminate(grace=0.2)
        assert backend.alive() and blocker.alive()
    finally:
        await backend.terminate(grace=0.2)
        await blocker.terminate(grace=0.2)


async def test_launch_refuses_empty_argv_and_missing_cwd(workdir: Path) -> None:
    from theater.daemon.harness_runtime.errors import BackendLaunchError

    with pytest.raises(BackendLaunchError, match="empty backend argv"):
        await launch_detached_backend(_plan([]), participant_id="p-empty", cwd=workdir)
    with pytest.raises(BackendLaunchError, match="does not exist"):
        await launch_detached_backend(
            _plan(_python(["-c", SLEEP_SNIPPET])),
            participant_id="p-nocwd",
            cwd=workdir / "missing",
        )


def test_capture_and_verify_a_foreign_process() -> None:
    # The identity helpers work against any process the OS can name.
    me = capture_process_identity(os.getpid())
    assert me.pid == os.getpid()
    assert me.start_token is not None
    verify_process_identity(me)
    assert process_start_token(os.getpid()) == me.start_token
