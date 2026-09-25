"""Detached native backend process ownership for one participant.

Own session so it outlives the daemon; every signal re-verifies pid + strong start identity
and a mismatch fails closed. SIGKILL only after SIGTERM, only via explicit teardown.
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
from urllib.parse import urlsplit

from theater import paths, proc
from theater.daemon.harness_runtime.constants import (
    RUNTIME_BACKEND_KILL_WAIT_SECONDS,
    RUNTIME_BACKEND_POLL_INTERVAL_SECONDS,
    RUNTIME_BACKEND_TERMINATE_GRACE_SECONDS,
    RUNTIME_ENDPOINT_DISCOVERY_DEADLINE_SECONDS,
    RUNTIME_ENDPOINT_DISCOVERY_LINE_MAX_BYTES,
    RUNTIME_ENDPOINT_DISCOVERY_MAX_BYTES,
    RUNTIME_ENDPOINT_DISCOVERY_POLL_SECONDS,
    RUNTIME_ENDPOINT_DISCOVERY_SETTLE_SECONDS,
    RUNTIME_SECRET_TOKEN_MAX_BYTES,
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
    return boot_time + ticks / clock_ticks


def _started_at_libproc(pid: int) -> float | None:
    """macOS start time with microsecond resolution, via libproc (``struct proc_bsdinfo``).

    ``ps -o lstart`` has one-second resolution, too weak against rapid pid reuse.
    None on any failure; the caller fails closed rather than use a weak identity.
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
    """The strong numeric start identity (persisted ``backend_started_at``), or ``None``.

    No weak fallback on purpose: an identity that cannot tell two same-second
    processes apart is no identity, so the caller fails closed.
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

    Pid reuse must never turn teardown into killing an unrelated process, and a pid
    we cannot strongly identify is a process we do not own.
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

    ``started_at`` is the persisted ``backend_started_at`` so a later daemon can re-verify;
    ``None`` means no strong identity and verification fails closed.
    """

    pid: int
    started_at: float | None = None
    comm: str | None = None


class DetachedBackendProcess:
    """One participant-owned detached backend; disconnecting never terminates it.

    An adopted handle (``_process`` None) checks identity for liveness, wait and signals; a
    mismatch means the process is gone — never a signal to whatever recycled the pid.
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
    def started_at(self) -> float | None:
        return self._identity.started_at

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

        A mismatch means the process is gone, never a licence to signal the pid's new holder.
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

        An adopted process is not our child, so its status is unobservable: return -1
        once identity stops holding, never an invented code.
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

        Identity is re-verified before each signal; a mismatch after the TERM grace means the
        process exited (pid maybe recycled), so SIGKILL is never sent. SIGKILL is only a fallback.
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

    The whole chain is symlink-checked before anything is created or chmod-ed through it,
    so private logs and secrets cannot be redirected outside the participant tree.
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

    Unreaped, so its pid cannot be reused meanwhile; never leave a running child behind a
    failed launch.
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


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})


def validate_discovered_endpoint(url: str) -> str:
    """Accept one literal loopback http URL, or fail closed.

    Anything with credentials, path, query, fragment or a non-loopback host is a shape
    Theater must not trust.
    """
    try:
        parsed = urlsplit(url, allow_fragments=False)
    except ValueError as exc:
        raise BackendLaunchError(
            f"discovered backend endpoint {url!r} is not a parseable URL ({exc}) — "
            "refuse to connect to an endpoint the plan did not document"
        ) from exc
    if parsed.scheme != "http":
        raise BackendLaunchError(
            f"discovered backend endpoint {url!r} is not http — loopback HTTP "
            "discovery never accepts another scheme"
        )
    if parsed.username or parsed.password:
        raise BackendLaunchError(
            f"discovered backend endpoint {url!r} carries credentials in the URL — "
            "secrets must stay in the private credential file, never the endpoint"
        )
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise BackendLaunchError(
            f"discovered backend endpoint {url!r} carries a path, query, or fragment — "
            "only a bare loopback origin may be discovered"
        )
    if parsed.hostname not in _LOOPBACK_HOSTS:
        raise BackendLaunchError(
            f"discovered backend endpoint {url!r} is not a literal loopback host — "
            "refuse to connect beyond the participant's own machine"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise BackendLaunchError(
            f"discovered backend endpoint {url!r} has no valid port ({exc})"
        ) from exc
    if port is None or not 1 <= port <= 65_535:
        raise BackendLaunchError(
            f"discovered backend endpoint {url!r} has no usable port — port 0 must "
            "be resolved by the backend itself, never reopened by Theater"
        )
    return url


def _read_new_stdout_bytes(stdout_path: Path, offset: int, limit: int) -> bytes:
    """Read only new bytes one discovery may still accept."""
    try:
        with stdout_path.open("rb") as handle:
            handle.seek(offset)
            return handle.read(limit)
    except OSError:
        return b""


def _consume_endpoint_lines(
    buffer: bytes,
    discovery,
    participant_id: str,
    endpoints: list[str],
) -> bytes:
    """Parse complete stdout lines; return the remaining partial buffer.

    Appends every validated endpoint to ``endpoints``; conflicting or
    malformed announcements raise fail-closed launch errors.
    """
    while b"\n" in buffer:
        line, buffer = buffer.split(b"\n", 1)
        if len(line) > RUNTIME_ENDPOINT_DISCOVERY_LINE_MAX_BYTES:
            raise BackendLaunchError(
                f"the detached backend for participant {participant_id} wrote an "
                f"oversized stdout line ({len(line)} bytes) — the documented "
                "endpoint contract is one bounded line; inspect the backend log"
            )
        text = line.decode("utf-8", errors="strict") if line.strip() else ""
        candidate = None
        if text:
            try:
                candidate = discovery.parser(text)
            except Exception as exc:
                raise BackendLaunchError(
                    f"the endpoint parser rejected stdout of participant {participant_id} "
                    f"({exc}) — a parser failure fails closed instead of guessing"
                ) from exc
        if candidate is not None:
            endpoint = validate_discovered_endpoint(str(candidate))
            if endpoints and endpoint not in endpoints:
                raise BackendLaunchError(
                    f"the detached backend for participant {participant_id} announced "
                    f"conflicting endpoints {endpoints[0]!r} and {endpoint!r} — "
                    "refuse to pick one; inspect the backend stdout log"
                )
            if endpoint not in endpoints:
                endpoints.append(endpoint)
    return buffer


async def _discover_stdout_endpoint(
    process: asyncio.subprocess.Process,
    stdout_path: Path,
    discovery,
    *,
    start_offset: int,
    participant_id: str,
) -> str:
    """Bounded post-launch discovery of this generation's endpoint line.

    Reads only bytes this generation appended; zero or conflicting endpoints fail closed
    after reaping the just-launched child.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + RUNTIME_ENDPOINT_DISCOVERY_DEADLINE_SECONDS
    max_bytes = min(discovery.max_bytes, RUNTIME_ENDPOINT_DISCOVERY_MAX_BYTES)
    buffer = b""
    bytes_read = 0
    endpoints: list[str] = []
    settle_deadline: float | None = None
    while True:
        if process.returncode is not None:
            raise BackendLaunchError(
                f"the detached backend for participant {participant_id} exited with "
                f"code {process.returncode} before announcing its endpoint — inspect "
                f"{stdout_path.name} and the backend stderr log before retrying"
            )
        remaining = max_bytes - bytes_read
        fresh = await asyncio.to_thread(
            _read_new_stdout_bytes,
            stdout_path,
            start_offset + bytes_read,
            remaining + 1,
        )
        if fresh:
            if len(fresh) > remaining:
                raise BackendLaunchError(
                    f"the detached backend for participant {participant_id} wrote more "
                    f"than {max_bytes} bytes without a usable endpoint line — refuse "
                    "to scan an unbounded log; inspect the backend stdout log"
                )
            bytes_read += len(fresh)
            buffer += fresh
            if len(buffer) > RUNTIME_ENDPOINT_DISCOVERY_LINE_MAX_BYTES and b"\n" not in buffer:
                raise BackendLaunchError(
                    f"the detached backend for participant {participant_id} wrote an "
                    f"oversized stdout line ({len(buffer)} bytes) — the documented "
                    "endpoint contract is one bounded line; inspect the backend log"
                )
            buffer = _consume_endpoint_lines(buffer, discovery, participant_id, endpoints)
            if endpoints and settle_deadline is None:
                settle_deadline = loop.time() + RUNTIME_ENDPOINT_DISCOVERY_SETTLE_SECONDS
        if endpoints and settle_deadline is not None and loop.time() >= settle_deadline:
            return endpoints[0]
        if loop.time() >= deadline:
            raise BackendLaunchError(
                f"the detached backend for participant {participant_id} did not announce "
                f"its endpoint within {RUNTIME_ENDPOINT_DISCOVERY_DEADLINE_SECONDS:.0f}s — "
                "terminate the just-launched backend; inspect its logs before retrying"
            )
        await asyncio.sleep(RUNTIME_ENDPOINT_DISCOVERY_POLL_SECONDS)


def _read_secret_token(token_path: Path, participant_id: str) -> str:
    """Read one private runtime secret for exec, refusing unsafe files.

    The password must never enter argv or logs, so its file gets the strictest checks.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(token_path, flags)
    except OSError as exc:
        raise BackendLaunchError(
            f"the runtime credential file {token_path} for participant "
            f"{participant_id} is missing ({exc}) — core must mint it before the "
            "backend launches; refusing to start without authentication"
        ) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise BackendLaunchError(
                f"the runtime credential file {token_path} for participant "
                f"{participant_id} is not a regular file — refusing"
            )
        if info.st_uid != os.geteuid():
            raise BackendLaunchError(
                f"the runtime credential file {token_path} for participant "
                f"{participant_id} is not owned by the daemon user — refusing"
            )
        if stat.S_IMODE(info.st_mode) & 0o177:
            raise BackendLaunchError(
                f"the runtime credential file {token_path} for participant "
                f"{participant_id} is too permissive — runtime secrets must be 0600"
            )
        raw = os.read(fd, RUNTIME_SECRET_TOKEN_MAX_BYTES + 1)
    except OSError as exc:
        raise BackendLaunchError(
            f"could not read the runtime credential file for participant "
            f"{participant_id}: {exc} — refusing to launch without authentication"
        ) from exc
    finally:
        os.close(fd)
    if not raw or len(raw) > RUNTIME_SECRET_TOKEN_MAX_BYTES or b"\n" in raw:
        raise BackendLaunchError(
            f"the runtime credential file for participant {participant_id} does not "
            "hold one bounded single-line token — refusing to launch"
        )
    try:
        return raw.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise BackendLaunchError(
            f"the runtime credential file for participant {participant_id} is not UTF-8 — "
            "refusing to launch"
        ) from exc


def _require_backend_endpoint(endpoint: str | None, participant_id: str) -> str:
    if endpoint is None:
        raise BackendLaunchError(
            f"runtime plan for participant {participant_id} has neither a fixed nor a "
            "discovered endpoint — refuse to launch a backend nothing can reach"
        )
    return endpoint


async def launch_detached_backend(
    plan: RuntimePlan,
    *,
    participant_id: str,
    cwd: Path,
) -> DetachedBackendProcess:
    """Launch one detached backend from a pure plan, owning its artifacts.

    Own session and participant-owned logs so daemon shutdown cannot take it down. No strong
    identity means terminate and reap: no later daemon could safely adopt or kill it.
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
    for key, token_path in backend.secret_env.items():
        # Resolved here, immediately before exec: token bytes enter only the
        # child environment — never argv, plan.env, or any repr.
        env[str(key)] = _read_secret_token(Path(token_path), participant_id)
    stdout_offset = stdout_path.stat().st_size
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
    try:
        identity = await asyncio.to_thread(capture_process_identity, process.pid)
        await asyncio.to_thread(_require_strong_identity, identity, participant_id=participant_id)
        endpoint = plan.endpoint
        if plan.endpoint_discovery is not None:
            endpoint = await _discover_stdout_endpoint(
                process,
                stdout_path,
                plan.endpoint_discovery,
                start_offset=stdout_offset,
                participant_id=participant_id,
            )
        endpoint = _require_backend_endpoint(endpoint, participant_id)
    except BaseException:
        await _reap_just_launched_child(process)
        raise
    return DetachedBackendProcess(
        process,
        identity,
        endpoint=endpoint,
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

    After a restart it is not our child; pid + ``backend_started_at`` are verified before
    any handle is returned, and a mismatch raises ``BackendIdentityMismatch`` registering nothing.
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
