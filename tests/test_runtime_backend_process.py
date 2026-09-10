"""Focused tests for detached backend process ownership.

Real processes (python subprocesses), real signals, real identity facts — the
launch path, session detachment, verified pid identity, graceful terminate,
kill fallback, and the fail-closed mismatch rule.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from theater import paths
from theater.daemon.harness_runtime import backend as harness_backend
from theater.daemon.harness_runtime.backend import (
    BackendProcessIdentity,
    DetachedBackendProcess,
    adopt_detached_backend,
    backend_artifacts_dir,
    capture_process_identity,
    launch_detached_backend,
    pid_alive,
    process_started_at,
    verify_process_identity,
)
from theater.daemon.harness_runtime.errors import (
    BackendIdentityMismatch,
    BackendLaunchError,
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


async def test_started_at_mismatch_is_a_mismatch_without_signaling(workdir: Path) -> None:
    backend = await _launch(_python(["-c", SLEEP_SNIPPET]), "p-token", workdir)
    try:
        wrong_started_at = BackendProcessIdentity(
            pid=backend.pid,
            comm=backend.identity.comm,
            started_at=(backend.identity.started_at or 0.0) + 123.456,
        )
        with pytest.raises(BackendIdentityMismatch, match="start identity"):
            verify_process_identity(wrong_started_at)
        assert backend.alive(), "a mismatch must never produce a signal"
        verify_process_identity(backend.identity)  # the real identity still passes
    finally:
        await backend.terminate()


def test_missing_strong_identity_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    me = capture_process_identity(os.getpid())
    assert me.started_at is not None, "this platform must provide a strong start identity"
    # A recorded identity with no strong start fact can never license a signal.
    with pytest.raises(BackendIdentityMismatch, match="no recorded strong start identity"):
        verify_process_identity(BackendProcessIdentity(pid=os.getpid()))
    # A platform that can no longer produce a strong reading fails closed too.
    monkeypatch.setattr(harness_backend, "process_started_at", lambda pid: None)
    with pytest.raises(BackendIdentityMismatch, match="can no longer be strongly identified"):
        verify_process_identity(me)
    monkeypatch.undo()
    verify_process_identity(me)  # with the strong reading back, identity holds


async def test_terminate_with_mismatched_identity_never_signals(workdir: Path) -> None:
    backend = await _launch(_python(["-c", IGNORE_SIGTERM_SNIPPET]), "p-mismatch", workdir)
    try:
        impostor = DetachedBackendProcess.__new__(DetachedBackendProcess)
        impostor._process = backend._process
        impostor._identity = BackendProcessIdentity(
            pid=backend.pid, comm="not-our-backend", started_at=backend.identity.started_at
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
            pid=blocker.pid, comm=backend.identity.comm, started_at=backend.identity.started_at
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
    assert isinstance(me.started_at, float), "the start identity is the persisted numeric"
    verify_process_identity(me)
    assert process_started_at(os.getpid()) == me.started_at


async def test_launch_without_a_strong_identity_reaps_the_child_and_fails(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An unidentifiable backend can never be safely signalled or adopted after
    # a restart, so the launch must fail closed — and never leave a child.
    spawned: list[int] = []
    real_capture = harness_backend.capture_process_identity

    def recording_capture(pid: int) -> BackendProcessIdentity:
        identity = real_capture(pid)
        spawned.append(pid)
        return BackendProcessIdentity(pid=identity.pid, comm=identity.comm, started_at=None)

    monkeypatch.setattr(harness_backend, "capture_process_identity", recording_capture)
    with pytest.raises(BackendLaunchError, match="strong start identity"):
        await launch_detached_backend(
            _plan(_python(["-c", SLEEP_SNIPPET])), participant_id="p-noid", cwd=workdir
        )
    assert spawned, "the child was started and its identity was examined"
    await _settle_pid_gone(spawned[0])
    assert not pid_alive(spawned[0]), "a failed launch must terminate and reap its child"


async def _settle_pid_gone(pid: int, attempts: int = 200) -> None:
    for _ in range(attempts):
        if not pid_alive(pid):
            return
        await asyncio.sleep(0.05)


# ---- adoption of an already-running backend ----------------------------------


async def test_adoption_from_persisted_identity_and_safe_teardown(workdir: Path) -> None:
    backend = await _launch(_python(["-c", SLEEP_SNIPPET]), "p-adopt", workdir)
    identity = backend.identity
    assert identity.started_at is not None
    try:
        # A fresh daemon knows only the persisted facts: pid, started_at, endpoint.
        adopted = adopt_detached_backend(
            identity.pid,
            started_at=identity.started_at,
            endpoint=backend.endpoint,
            participant_id="p-adopt",
        )
        assert adopted.pid == identity.pid
        assert adopted.identity.started_at == identity.started_at
        assert adopted.alive()
        verify_process_identity(adopted.identity)
        # Teardown of the adopted handle terminates the real process, once.
        await adopted.terminate(grace=2.0)
        assert not adopted.alive()
    finally:
        await backend.wait()
    assert not pid_alive(identity.pid)


async def test_adoption_of_a_mismatched_identity_fails_closed(workdir: Path) -> None:
    backend = await _launch(_python(["-c", SLEEP_SNIPPET]), "p-adopt2", workdir)
    try:
        with pytest.raises(BackendIdentityMismatch, match="start identity changed"):
            adopt_detached_backend(
                backend.pid,
                started_at=(backend.identity.started_at or 0.0) + 999.0,
                endpoint=backend.endpoint,
                participant_id="p-adopt2",
            )
        assert backend.alive(), "a mismatched adoption must never signal anything"
    finally:
        await backend.terminate(grace=0.5)


async def test_adoption_of_a_dead_pid_fails_closed(workdir: Path) -> None:
    backend = await _launch(_python(["-c", SLEEP_SNIPPET]), "p-adopt3", workdir)
    pid = backend.pid
    await backend.terminate(grace=0.5)
    await _settle_pid_gone(pid)
    with pytest.raises(BackendIdentityMismatch, match="gone"):
        adopt_detached_backend(
            pid, started_at=1.0, endpoint=backend.endpoint, participant_id="p-adopt3"
        )


async def test_adopted_liveness_treats_identity_loss_as_gone(workdir: Path) -> None:
    backend = await _launch(_python(["-c", IGNORE_SIGTERM_SNIPPET]), "p-adopt4", workdir)
    output = await asyncio.to_thread(_wait_for_line, backend.stdout_path, b"ready")
    assert b"ready" in output
    adopted = adopt_detached_backend(
        backend.pid,
        started_at=backend.identity.started_at,
        endpoint=backend.endpoint,
        participant_id="p-adopt4",
    )
    assert adopted.alive()
    os.kill(backend.pid, signal.SIGKILL)
    await backend.wait()
    assert not adopted.alive(), "the owned process is gone; never signal what recycled the pid"
    # Signalling through the adopted handle now fails closed, no signal.
    with pytest.raises(BackendIdentityMismatch):
        await adopted.terminate(grace=0.2)


# ---- artifact hardening -------------------------------------------------------


async def test_artifacts_reject_a_symlinked_runtime_dir(workdir: Path) -> None:
    runtime_dir = paths.participant_dir("p-symlink") / "runtime"
    runtime_dir.parent.mkdir(parents=True, exist_ok=True)
    elsewhere = workdir / "elsewhere"
    elsewhere.mkdir()
    runtime_dir.symlink_to(elsewhere)
    try:
        with pytest.raises(OSError, match="not a real directory"):
            await _launch(_python(["-c", SLEEP_SNIPPET]), "p-symlink", workdir)
    finally:
        runtime_dir.unlink()


async def test_artifacts_enforce_private_permissions_on_existing_dirs(workdir: Path) -> None:
    directory = backend_artifacts_dir("p-perms")
    directory.chmod(0o755)  # pre-existing, too loose
    backend = await _launch(_python(["-c", SLEEP_SNIPPET]), "p-perms", workdir)
    try:
        assert stat.S_IMODE(directory.stat().st_mode) & 0o777 == 0o700
        assert stat.S_IMODE(directory.parent.stat().st_mode) & 0o777 == 0o700
    finally:
        await backend.terminate()


async def test_launch_closes_the_first_log_fd_when_the_second_open_fails(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("lsof") is None:
        pytest.skip("lsof is required to observe file descriptors")
    stderr_opens = 0
    real_open = os.open

    def failing_second_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal stderr_opens
        if str(path).endswith("backend.stderr.log"):
            stderr_opens += 1
            if stderr_opens == 2:  # the launch's append-open, not ensure_private_file
                raise OSError("the second log open failed")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", failing_second_open)
    with pytest.raises(OSError, match="the second log open failed"):
        await _launch(_python(["-c", SLEEP_SNIPPET]), "p-fd", workdir)
    monkeypatch.undo()
    stdout_log = backend_artifacts_dir("p-fd") / "backend.stdout.log"
    lsof = await asyncio.to_thread(
        subprocess.run,
        ["lsof", "-p", str(os.getpid()), "-F", "n"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert str(stdout_log) not in lsof.stdout, "the first log fd must not leak"
