"""Detached native backend process ownership for one participant.

One isolated backend per participant, launched from a pure ``RuntimePlan``:

* the backend runs in its own session (``start_new_session=True``), so its
  lifetime never depends on daemon pipes or daemon shutdown — the UI and
  Theater share it and both survive a daemon restart;
* plan files and private secrets are written through the same validated,
  symlink-checked writer the spawner uses, into the participant's private
  tree;
* stdout/stderr land in participant-owned log files, never in daemon pipes;
* the recorded process identity is the pid plus the observed process name and
  start token — before any signal, ``verify_process_identity`` re-checks that
  the pid still names *our* backend, and a mismatch fails closed: no
  attachment, no signal, ever.

Termination is graceful-first (SIGTERM to the backend's process group), with
SIGKILL only after the grace elapses, and only through the manager's explicit
teardown — closing a runtime connection never touches the backend.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
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
from theater.daemon.spawning.planning import write_plan_files
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


def _start_token_linux(pid: int) -> str | None:
    stat_path = Path(f"/proc/{pid}/stat")
    if not stat_path.exists():
        return None
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
    return fields[19]


def _start_token_libproc(pid: int) -> str | None:
    """macOS process start time with microsecond resolution, via libproc.

    ``ps -o lstart`` only has one-second resolution, which cannot distinguish
    two processes started in the same second — exactly the rapid crash-restart
    pattern where pid reuse would matter. ``proc_pidinfo`` exposes the real
    start timestamp; the field layout follows ``struct proc_bsdinfo`` in
    ``sys/proc_info.h``. Returns None on any mismatch or failure, and the
    caller falls back to the coarse token rather than guessing.
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
        token = f"{info.pbi_start_tvsec}.{info.pbi_start_tvusec:06d}"
    except Exception:
        return None
    else:
        return token


def _start_token_ps(pid: int) -> str | None:
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip() or None


def process_start_token(pid: int) -> str | None:
    """One opaque token identifying a pid's process start, for pid-reuse detection.

    The token is never parsed into a time — only compared. Linux reads the
    ``/proc`` start time in clock ticks; macOS reads the microsecond start
    timestamp through libproc (``ps lstart`` alone cannot distinguish two
    processes started in the same second); other systems fall back to the raw
    ``ps lstart`` string. A changed token means the pid was recycled.
    """
    return _start_token_linux(pid) or _start_token_libproc(pid) or _start_token_ps(pid)


def capture_process_identity(pid: int) -> BackendProcessIdentity:
    """Record the identity facts that let a later check prove pid ownership."""
    try:
        comm = proc.comm(pid)
    except Exception:
        comm = None
    return BackendProcessIdentity(pid=pid, comm=comm, start_token=process_start_token(pid))


def verify_process_identity(identity: BackendProcessIdentity) -> None:
    """Prove the pid still names our backend, or fail closed with no signal.

    ``BackendIdentityMismatch`` means: do not attach, do not signal, reconcile
    from persisted facts instead. A dead pid, a changed start token, or a
    process name that no longer matches the recorded backend are all
    mismatches — pid reuse must never turn teardown into killing an unrelated
    process.
    """
    if not pid_alive(identity.pid):
        raise BackendIdentityMismatch(
            f"backend pid {identity.pid} is gone — refuse to signal or attach; reconcile "
            "from the persisted runtime binding instead of guessing at a replacement"
        )
    if identity.start_token is not None:
        current = process_start_token(identity.pid)
        if current != identity.start_token:
            raise BackendIdentityMismatch(
                f"pid {identity.pid} now identifies a different process (start token "
                "changed) — the recorded backend exited and the pid was reused; never "
                "signal a process we do not own"
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
    """Verified process identity: pid plus the facts that pin it to one process."""

    pid: int
    comm: str | None = None
    start_token: str | None = None


class DetachedBackendProcess:
    """One participant-owned detached backend process handle.

    The handle proves identity before every signal and never terminates as a
    side effect of disconnecting — only ``terminate`` (graceful, then kill)
    ends the process, and only the manager's explicit teardown calls it.
    """

    def __init__(
        self,
        process: asyncio.subprocess.Process,
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

    def alive(self) -> bool:
        """Whether the backend is still running (a reaped exit is not alive)."""
        return self._process.returncode is None and pid_alive(self._identity.pid)

    async def wait(self) -> int:
        """Wait for exit and return the exit code (our child, so always reapable)."""
        return await self._process.wait()

    async def terminate(
        self,
        *,
        grace: float = RUNTIME_BACKEND_TERMINATE_GRACE_SECONDS,
    ) -> None:
        """Gracefully terminate the verified backend's process group.

        Identity is verified first: a mismatch raises before any signal is
        sent, so a reused pid can never receive our SIGTERM. SIGKILL is the
        fallback after the grace period, never the first move.
        """
        await asyncio.to_thread(verify_process_identity, self._identity)
        await self._signal_group(signal.SIGTERM)
        if await self._wait_gone(grace):
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
            if self._process.returncode is not None:
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(RUNTIME_BACKEND_POLL_INTERVAL_SECONDS)


def backend_artifacts_dir(participant_id: str) -> Path:
    """The participant's private directory for detached-backend artifacts."""
    directory = paths.participant_dir(participant_id) / "runtime"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


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
    stderr_fd = os.open(stderr_path, append_flags, 0o600)
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
    return DetachedBackendProcess(
        process,
        identity,
        endpoint=plan.endpoint,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )


__all__ = [
    "BackendProcessIdentity",
    "DetachedBackendProcess",
    "backend_artifacts_dir",
    "capture_process_identity",
    "launch_detached_backend",
    "pid_alive",
    "process_start_token",
    "verify_process_identity",
]
