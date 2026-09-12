"""Detached native backend process ownership for one participant.

One isolated backend per participant, launched from a pure ``RuntimePlan``:

* the backend runs in its own session (``start_new_session=True``), so its
  lifetime never depends on daemon pipes or daemon shutdown — the UI and
  Theater share it and both survive a daemon restart;
* plan files and private secrets are written through the same validated,
  symlink-checked writer the spawner uses, into the participant's private
  tree;
* stdout/stderr land in participant-owned log files, never in daemon pipes;
* the recorded process identity is the pid plus the strong numeric start
  identity (the persisted ``backend_started_at``) and the observed process
  name — before any signal, ``verify_process_identity`` re-checks that the
  pid still names *our* backend, and a mismatch fails closed: no attachment,
  no signal, ever. A launch that cannot establish a strong start identity
  terminates and reaps the child and fails;
* a fresh daemon can adopt an already-running backend from the persisted
  pid + start identity + endpoint (``adopt_detached_backend``): the handle
  has no child-process object, so liveness, waiting, and every signal go
  through identity verification, and an identity mismatch means the owned
  process is gone — never a signal to whatever recycled the pid.

Termination is graceful-first (SIGTERM to the backend's process group), with
SIGKILL only after the grace elapses, and only through the manager's explicit
teardown — closing a runtime connection never touches the backend.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from theater import paths, proc
from theater.daemon.harness_runtime.constants import (
    RUNTIME_BACKEND_KILL_WAIT_SECONDS,
    RUNTIME_BACKEND_POLL_INTERVAL_SECONDS,
    RUNTIME_BACKEND_TERMINATE_GRACE_SECONDS,
)
from theater.daemon.harness_runtime.errors import (
    BackendIdentityMismatch,
    BackendLaunchError,
    BackendProcessError,
)
from theater.harness.contracts.runtime import RuntimePlan


def pid_alive(pid: int) -> bool:
    """Whether a pid names a live process (zombies count as live)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _boot_time_linux() -> float | None:
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _started_at_linux(pid: int) -> float | None:
    """Linux: boot time plus the /proc start time in ticks, as epoch seconds."""
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        raw = stat_path.read_text()
    except OSError:
        return None
    # The comm field may contain spaces and parentheses; the start time is the
    # 22nd field after the final ')' in the command name.
    marker = raw.rfind(")")
    if marker < 0:
        return None
    fields = raw[marker + 2 :].split()
    if len(fields) < 20:
        return None
    try:
        ticks = int(fields[19])
    except ValueError:
        return None
    boot_time = _boot_time_linux()
    if boot_time is None:
        return None
    try:
        clock_ticks = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError):
        return None
    if clock_ticks <= 0:
        return None
    return boot_time + ticks / float(clock_ticks)


def _started_at_libproc(pid: int) -> float | None:
    """macOS process start time with microsecond resolution, via libproc.

    ``ps -o lstart`` only has one-second resolution, which cannot distinguish
    two processes started in the same second — exactly the rapid crash-restart
    pattern where pid reuse would matter. ``proc_pidinfo`` exposes the real
    start timestamp; the field layout follows ``struct proc_bsdinfo`` in
    ``sys/proc_info.h``. Returns None on any mismatch or failure, and the
    caller fails closed rather than falling back to a weak identity.
    """
    try:
        import ctypes
        from ctypes import c_char, c_int32, c_uint32, c_uint64

        class _ProcBsdInfo(ctypes.Structure):
            _fields_ = [
                ("pbi_flags", c_uint32),
                ("pbi_status", c_uint32),
                ("pbi_xstatus", c_uint32),
                ("pbi_pid", c_uint32),
                ("pbi_ppid", c_uint32),
                ("pbi_uid", c_uint32),
                ("pbi_gid", c_uint32),
                ("pbi_ruid", c_uint32),
                ("pbi_rgid", c_uint32),
                ("pbi_svuid", c_uint32),
                ("pbi_svgid", c_uint32),
                ("rfu_1", c_uint32),
                ("pbi_comm", c_char * 16),
                ("pbi_name", c_char * 32),
                ("pbi_nfiles", c_uint32),
                ("pbi_pgid", c_uint32),
                ("pbi_pjobc", c_uint32),
                ("e_tdev", c_uint32),
                ("e_tpgid", c_uint32),
                ("pbi_nice", c_int32),
                ("pbi_start_tvsec", c_uint64),
                ("pbi_start_tvusec", c_uint64),
            ]

        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        info = _ProcBsdInfo()
        written = libproc.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if written <= 0 or info.pbi_pid != pid:
            return None
        started_at = float(info.pbi_start_tvsec) + info.pbi_start_tvusec / 1_000_000.0
    except Exception:
        return None
    else:
        return started_at


def process_started_at(pid: int) -> float | None:
    """The strong numeric start identity of one pid, or ``None``.

    This is the value persisted as ``backend_started_at`` (the frozen
    ``participant_runtime_bindings`` column): a stable float the next daemon
    can read back and re-verify against the same live process after a
    restart. Linux computes boot time plus the /proc clock-tick start time;
    macOS reads the microsecond start timestamp through libproc. There is no
    weak fallback on purpose: a start identity that cannot distinguish two
    processes started in the same second is not identity at all, so platforms
    without a strong reading get ``None`` and the caller fails closed.
    """
    return _started_at_libproc(pid) or _started_at_linux(pid)


def capture_process_identity(pid: int) -> BackendProcessIdentity:
    """Record the identity facts that let a later check prove pid ownership."""
    try:
        comm = proc.comm(pid)
    except Exception:
        comm = None
    return BackendProcessIdentity(pid=pid, comm=comm, started_at=process_started_at(pid))


def verify_process_identity(identity: BackendProcessIdentity) -> None:
    """Prove the pid still names our backend, or fail closed with no signal.

    ``BackendIdentityMismatch`` means: do not attach, do not signal, reconcile
    from persisted facts instead. A dead pid, a missing strong start identity,
    a changed start timestamp, or a process name that no longer matches the
    recorded backend are all mismatches — pid reuse must never turn teardown
    into killing an unrelated process, and a pid we cannot strongly identify
    is a process we do not own.
    """
    if not pid_alive(identity.pid):
        raise BackendIdentityMismatch(
            f"backend pid {identity.pid} is gone — refuse to signal or attach; reconcile "
            "from the persisted runtime binding instead of guessing at a replacement"
        )
    if identity.started_at is None:
        raise BackendIdentityMismatch(
            f"backend pid {identity.pid} has no recorded strong start identity — "
            "pid-plus-comm alone cannot prove ownership, so nothing may be signalled; "
            "reconcile from the persisted runtime binding"
        )
    current = process_started_at(identity.pid)
    if current is None:
        raise BackendIdentityMismatch(
            f"backend pid {identity.pid} can no longer be strongly identified "
            "(no process start timestamp) — refuse to signal a process we cannot prove "
            "we own"
        )
    if current != identity.started_at:
        raise BackendIdentityMismatch(
            f"pid {identity.pid} now identifies a different process (start identity "
            f"changed from {identity.started_at!r} to {current!r}) — the recorded "
            "backend exited and the pid was reused; never signal a process we do not own"
        )
    if identity.comm is not None:
        try:
            observed = proc.comm(identity.pid)
        except Exception:
            observed = None
        if observed is not None and observed != identity.comm:
            raise BackendIdentityMismatch(
                f"pid {identity.pid} reports process name {observed!r}, not the recorded "
                f"backend {identity.comm!r} — refuse to signal a process we do not own"
            )


@dataclass(frozen=True, slots=True)
class BackendProcessIdentity:
    """Verified process identity: pid plus the facts that pin it to one process.

    ``started_at`` is the strong numeric start identity — the exact float
    persisted as ``backend_started_at`` in ``participant_runtime_bindings``,
    so a later daemon can reconstruct this identity from storage after a
    restart and re-verify the same live process. ``None`` means no strong
    identity was captured, and verification fails closed.
    """

    pid: int
    started_at: float | None = None
    comm: str | None = None


class DetachedBackendProcess:
    """One participant-owned detached backend process handle.

    The handle proves identity before every signal and never terminates as a
    side effect of disconnecting — only ``terminate`` (graceful, then kill)
    ends the process, and only the manager's explicit teardown calls it.

    A handle either owns the process as its child (``_process`` is set, the
    pid cannot be reused before the child is reaped) or *adopted* it from
    persisted identity facts after a daemon restart (``_process`` is None):
    then liveness, waiting, and teardown all go through process identity, and
    an identity mismatch means the owned process is gone — never a signal to
    whatever recycled the pid.
    """

    def __init__(
        self,
        process: asyncio.subprocess.Process | None,
        identity: BackendProcessIdentity,
        *,
        endpoint: str,
        stdout_path: Path,
        stderr_path: Path,
    ) -> None:
        self._process = process
        self._identity = identity
        self._endpoint = endpoint
        self._stdout_path = stdout_path
        self._stderr_path = stderr_path

    @property
    def identity(self) -> BackendProcessIdentity:
        return self._identity

    @property
    def pid(self) -> int:
        return self._identity.pid

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def stdout_path(self) -> Path:
        return self._stdout_path

    @property
    def stderr_path(self) -> Path:
        return self._stderr_path

    def identity_holds(self) -> bool:
        """Whether the recorded identity still names the process we own.

        For adopted processes this is liveness: a dead pid or a changed start
        identity means the owned process is gone — it is not an error, and it
        is never a licence to signal whatever now holds the pid.
        """
        try:
            verify_process_identity(self._identity)
        except BackendProcessError:
            return False
        return True

    def alive(self) -> bool:
        """Whether the backend is still running (a reaped exit is not alive)."""
        if self._process is not None:
            return self._process.returncode is None and pid_alive(self._identity.pid)
        return self.identity_holds()

    async def wait(self) -> int:
        """Wait for exit and return the exit code.

        Our child is always reapable, so a child handle waits for the real
        status. An adopted process is not our child: the previous parent (or
        init) reaps it, so there is no status to observe — wait until the
        identity stops holding and return -1 (unknown status), never an
        invented code.
        """
        if self._process is not None:
            return await self._process.wait()
        await asyncio.to_thread(self._await_identity_gone)
        return -1

    async def terminate(
        self,
        *,
        grace: float = RUNTIME_BACKEND_TERMINATE_GRACE_SECONDS,
    ) -> None:
        """Gracefully terminate the verified backend's process group.

        Identity is verified immediately before every signal: a mismatch
        raises before SIGTERM, so a reused pid can never receive it, and a
        mismatch discovered after the TERM grace means the owned process
        exited during the grace (the pid may already be recycled), so
        SIGKILL is never sent. SIGKILL is the fallback after the grace
        period, never the first move.
        """
        await asyncio.to_thread(verify_process_identity, self._identity)
        await self._signal_group(signal.SIGTERM)
        if await self._wait_gone(grace):
            return
        if not await asyncio.to_thread(self.identity_holds):
            # The backend exited during the grace; a reused pid must never
            # receive the SIGKILL meant for the process that just died.
            return
        await self._signal_group(signal.SIGKILL)
        if not await self._wait_gone(RUNTIME_BACKEND_KILL_WAIT_SECONDS):
            raise BackendProcessError(
                f"backend pid {self.pid} survived SIGKILL — do not force anything else; "
                "inspect the process manually before removing participant state"
            )

    async def kill(self) -> None:
        """SIGKILL the verified backend's process group, without a TERM grace."""
        await asyncio.to_thread(verify_process_identity, self._identity)
        await self._signal_group(signal.SIGKILL)
        if not await self._wait_gone(RUNTIME_BACKEND_KILL_WAIT_SECONDS):
            raise BackendProcessError(
                f"backend pid {self.pid} survived SIGKILL — do not force anything else; "
                "inspect the process manually before removing participant state"
            )

    async def _signal_group(self, sig: int) -> None:
        # start_new_session made the backend its own session and group leader,
        # so the pid doubles as the pgid and the whole backend tree gets the
        # signal; a ProcessLookupError means it is already gone.
        try:
            await asyncio.to_thread(os.killpg, self._identity.pid, sig)
        except ProcessLookupError:
            return

    async def _wait_gone(self, timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if self._gone():
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(RUNTIME_BACKEND_POLL_INTERVAL_SECONDS)

    def _gone(self) -> bool:
        if self._process is not None:
            return self._process.returncode is not None
        # Adopted: gone exactly when the identity stops holding.
        return not self.identity_holds()

    def _await_identity_gone(self) -> None:
        while self.identity_holds():
            time.sleep(RUNTIME_BACKEND_POLL_INTERVAL_SECONDS)


def backend_artifacts_dir(participant_id: str) -> Path:
    """The participant's private directory for detached-backend artifacts.

    Every path component from the participants root down to the runtime
    directory is checked *before* anything is created or chmod-ed through it:
    a symlink anywhere on that chain could redirect private logs and secrets
    outside the participant tree, so a bad chain is rejected without being
    touched first. Private permissions (0o700) are then enforced on the
    participant-owned directories, even when they already exist.
    """
    directory = paths.participant_dir(participant_id) / "runtime"
    chain = [paths.participants_dir(), directory.parent, directory]
    for path in chain:
        try:
            mode = path.lstat().st_mode
        except OSError:
            continue  # not created yet; the preflight of its parents governs
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise OSError(
                f"backend artifact path {path} is not a real directory "
                "(symlinks are rejected: private logs and secrets must stay inside "
                "the participant tree)"
            )
    # Nothing existing on the chain is a symlink or a non-directory, so
    # creating through it cannot land outside the participant tree.
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in chain[1:]:
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise OSError(
                f"backend artifact path {path} is not a real directory "
                "(symlinks are rejected: private logs and secrets must stay inside "
                "the participant tree)"
            )
        path.chmod(0o700)
    return directory


def _require_strong_identity(
    identity: BackendProcessIdentity,
    *,
    participant_id: str,
) -> None:
    if identity.started_at is None:
        raise BackendLaunchError(
            f"the detached backend for participant {participant_id} (pid "
            f"{identity.pid}) could not be given a strong start identity on this "
            "platform — an unidentifiable backend can never be safely signalled or "
            "adopted after a restart, so the launch fails closed"
        )


async def _reap_just_launched_child(process: asyncio.subprocess.Process) -> None:
    """Terminate and reap a child we are about to abandon, bounded in time.

    The child is ours and unreaped, so its pid cannot be reused while we do
    this: SIGTERM, wait for the grace, SIGKILL, wait again. Used when a launch
    must fail after the process already started — never leave a running child
    behind a failed launch.
    """
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), RUNTIME_BACKEND_TERMINATE_GRACE_SECONDS)
    except TimeoutError:
        pass
    else:
        return
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    await asyncio.wait_for(process.wait(), RUNTIME_BACKEND_KILL_WAIT_SECONDS)


async def launch_detached_backend(
    plan: RuntimePlan,
    *,
    participant_id: str,
    cwd: Path,
) -> DetachedBackendProcess:
    """Launch one detached backend from a pure plan, owning its artifacts.

    Plan files are written before the process starts, stdout/stderr are
    participant-owned log files (never daemon pipes), and the process starts
    in its own session so daemon shutdown cannot take the backend with it.
    A launch that cannot establish a strong process identity terminates and
    reaps the child and fails: an unidentifiable backend must never be left
    running, because no later daemon could safely adopt or terminate it.
    """
    if not isinstance(plan, RuntimePlan):
        raise TypeError("detached backend launch requires a RuntimePlan")
    backend = plan.backend
    argv = [str(part) for part in backend.argv]
    if not argv:
        raise BackendLaunchError(
            f"runtime plan for participant {participant_id} has an empty backend argv — "
            "the plugin planner produced no command to run; refuse to launch nothing"
        )
    if not await asyncio.to_thread(cwd.is_dir):
        raise BackendLaunchError(
            f"backend cwd {cwd} does not exist for participant {participant_id} — create "
            "the worktree before launching the backend"
        )
    from theater.daemon.spawning.planning import write_plan_files

    write_plan_files(backend)
    artifacts = backend_artifacts_dir(participant_id)
    stdout_path = artifacts / "backend.stdout.log"
    stderr_path = artifacts / "backend.stderr.log"
    paths.ensure_private_file(stdout_path)
    paths.ensure_private_file(stderr_path)
    env = dict(os.environ)
    for key, value in backend.env.items():
        env[str(key)] = str(value)
    append_flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    stdout_fd = os.open(stdout_path, append_flags, 0o600)
    try:
        stderr_fd = os.open(stderr_path, append_flags, 0o600)
    except OSError:
        os.close(stdout_fd)
        raise
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=stdout_fd,
            stderr=stderr_fd,
            start_new_session=True,
        )
    except OSError as exc:
        raise BackendLaunchError(
            f"failed to launch the detached backend for participant {participant_id} "
            f"({argv[0]!r}): {exc} — no backend process was started, so persist the "
            "failure and stop; never relaunch after an ambiguous dispatch"
        ) from exc
    finally:
        os.close(stdout_fd)
        os.close(stderr_fd)
    identity = await asyncio.to_thread(capture_process_identity, process.pid)
    try:
        await asyncio.to_thread(_require_strong_identity, identity, participant_id=participant_id)
    except BackendLaunchError:
        await _reap_just_launched_child(process)
        raise
    return DetachedBackendProcess(
        process,
        identity,
        endpoint=plan.endpoint,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )


def adopt_detached_backend(
    pid: int,
    *,
    started_at: float,
    endpoint: str,
    participant_id: str,
) -> DetachedBackendProcess:
    """Adopt an already-running detached backend from persisted identity.

    After a daemon restart the backend is not our child, so there is no
    ``asyncio.subprocess.Process`` handle — ownership is re-established from
    the persisted facts alone: the pid plus the strong numeric start identity
    (``backend_started_at``) recorded when the backend launched. The persisted
    identity is verified against the live process *before* any handle is
    returned: a dead pid or a changed start identity raises
    ``BackendIdentityMismatch``, and nothing is registered. The returned
    handle can be reconnected to and later terminated safely; while it lives,
    liveness and every signal go through identity verification.
    """
    persisted = BackendProcessIdentity(pid=pid, started_at=started_at)
    verify_process_identity(persisted)
    identity = capture_process_identity(pid)
    # The persisted facts remain the authority; the freshly captured comm only
    # adds one more fact the next verification can check.
    identity = BackendProcessIdentity(pid=pid, started_at=started_at, comm=identity.comm)
    artifacts = backend_artifacts_dir(participant_id)
    return DetachedBackendProcess(
        None,
        identity,
        endpoint=endpoint,
        stdout_path=artifacts / "backend.stdout.log",
        stderr_path=artifacts / "backend.stderr.log",
    )


__all__ = [
    "BackendProcessIdentity",
    "DetachedBackendProcess",
    "adopt_detached_backend",
    "backend_artifacts_dir",
    "capture_process_identity",
    "launch_detached_backend",
    "pid_alive",
    "process_started_at",
    "verify_process_identity",
]
