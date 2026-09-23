"""Standalone daemon startup and non-destructive background bridge control."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import json
import os
import secrets
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from regie.contracts import BridgeConfig, BridgeStatus
from regie.paths import RegiePaths
from theater.frontend import (
    FrontendClient,
    FrontendResponseError,
    HandshakeResult,
    NegotiationError,
    RequestTimedOut,
    TransportConnectionError,
)

_DEFAULT_START_TIMEOUT = 10.0
_POLL_SECONDS = 0.05
_BRIDGE_WORKER_MODULE = "regie"


class RegieStartupError(RuntimeError):
    """The standalone launcher could not safely establish its required services."""


class IncompatibleDaemon(RegieStartupError):
    """A reachable daemon cannot provide Régie's frozen public API."""


class BridgeStartupError(RegieStartupError):
    """A bridge did not become ready within its bounded startup window."""


class _Popen(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...


@dataclass(frozen=True, slots=True)
class BridgeProcessStatus:
    """Read-only process and latest bridge connection state."""

    running: bool
    connection_state: str
    pid: int | None = None
    provider_id: str | None = None
    provider_generation: int | None = None
    tmux_server_identity: str | None = None
    detail: str | None = None
    token: str | None = None

    @classmethod
    def stopped(cls, detail: str | None = None) -> BridgeProcessStatus:
        return cls(running=False, connection_state="stopped", detail=detail)


class _FileLock:
    """A small flock wrapper; only bridge workers retain the process lock."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._file: Any | None = None

    def acquire(self) -> None:
        descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        handle = os.fdopen(descriptor, "r+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise BridgeStartupError(
                "another Régie bridge process is already starting or running"
            ) from None
        os.fchmod(handle.fileno(), 0o600)
        self._file = handle

    def acquire_blocking(self) -> None:
        descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        handle = os.fdopen(descriptor, "r+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        os.fchmod(handle.fileno(), 0o600)
        self._file = handle

    def release(self) -> None:
        handle, self._file = self._file, None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


async def connect_or_start_daemon(
    *,
    socket_path: Path,
    client_id: str,
    required_capabilities: Sequence[str],
    log_path: Path,
    timeout: float = _DEFAULT_START_TIMEOUT,
    client_factory: Callable[..., FrontendClient] = FrontendClient,
    popen_factory: Callable[..., _Popen] = subprocess.Popen,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> tuple[FrontendClient, HandshakeResult]:
    """Connect to a compatible daemon, starting this installed Theater only when absent."""
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("timeout must be a positive number")
    try:
        async with asyncio.timeout(timeout):
            return await _connect(
                socket_path,
                client_id,
                required_capabilities,
                request_timeout=timeout,
                client_factory=client_factory,
            )
    except TimeoutError as exc:
        raise RegieStartupError(
            f"Theater daemon did not answer within {timeout:g}s; refusing to replace it"
        ) from exc
    except RequestTimedOut as exc:
        raise RegieStartupError(
            f"Theater daemon did not answer within {timeout:g}s; refusing to replace it"
        ) from exc
    except Exception as exc:
        if not _daemon_absent(exc):
            raise _incompatible(exc) from exc

    _start_daemon(log_path, popen_factory=popen_factory)
    deadline = monotonic() + timeout
    last: Exception | None = None
    while monotonic() < deadline:
        try:
            remaining = max(0.001, deadline - monotonic())
            async with asyncio.timeout(remaining):
                return await _connect(
                    socket_path,
                    client_id,
                    required_capabilities,
                    request_timeout=remaining,
                    client_factory=client_factory,
                )
        except TimeoutError:
            break
        except RequestTimedOut as exc:
            last = exc
            break
        except Exception as exc:
            if not _daemon_absent(exc):
                raise _incompatible(exc) from exc
            last = exc
            await sleep(_POLL_SECONDS)
    raise RegieStartupError(
        f"Theater daemon did not become available within {timeout:g}s; see {log_path}"
    ) from last


async def _connect(
    socket_path: Path,
    client_id: str,
    required_capabilities: Sequence[str],
    *,
    request_timeout: float,
    client_factory: Callable[..., FrontendClient],
) -> tuple[FrontendClient, HandshakeResult]:
    client = client_factory(
        socket_path,
        client_id=client_id,
        required_capabilities=required_capabilities,
        request_timeout=request_timeout,
    )
    try:
        handshake = await client.connect()
    except BaseException:
        await client.close()
        raise
    return client, handshake


def _daemon_absent(error: Exception) -> bool:
    if isinstance(error, (FileNotFoundError, ConnectionRefusedError)):
        return True
    if not isinstance(error, TransportConnectionError):
        return False
    cause = error.__cause__
    return isinstance(cause, OSError) and cause.errno in {errno.ENOENT, errno.ECONNREFUSED}


def _incompatible(error: Exception) -> IncompatibleDaemon:
    if isinstance(error, (NegotiationError, FrontendResponseError)):
        detail = str(error)
    else:
        detail = f"{type(error).__name__}: {error}"
    return IncompatibleDaemon(
        "The running Theater daemon is not compatible with Régie's required public API or "
        f"capabilities ({detail}). Upgrade or start matching Theater and Régie versions; "
        "Régie will not replace a running daemon."
    )


def _start_daemon(log_path: Path, *, popen_factory: Callable[..., _Popen]) -> _Popen:
    log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    output = os.fdopen(descriptor, "ab", closefd=True)
    try:
        return popen_factory(
            [sys.executable, "-m", "theater.cli", "daemon"],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            start_new_session=True,
            env=os.environ.copy(),
        )
    finally:
        output.close()


class BridgeProcessManager:
    """Start, inspect, and stop only the dedicated bridge child process."""

    def __init__(
        self,
        paths: RegiePaths,
        *,
        socket_path: Path,
        selector: str = "tmux",
        client_id: str = "regie-tmux-bridge",
        popen_factory: Callable[..., _Popen] = subprocess.Popen,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._paths = paths
        self._socket_path = socket_path
        self._selector = selector
        self._client_id = client_id
        self._popen = popen_factory
        self._monotonic = monotonic
        self._sleep = sleep

    def status(self) -> BridgeProcessStatus:
        """Inspect private process facts only; this never contacts or starts Theater."""
        raw = _read_status(self._paths.bridge_status_path)
        if raw is None:
            return BridgeProcessStatus.stopped()
        status = _status_from_wire(raw)
        if (
            not status.running
            or status.pid is None
            or status.token is None
            or _read_pid(self._paths.bridge_pid_path) != (status.pid, status.token)
            or not _pid_alive(status.pid)
            or not _lock_held(self._paths.bridge_process_lock)
            or not _bridge_worker_matches(status.pid, status.token)
        ):
            return BridgeProcessStatus.stopped("the recorded bridge process is no longer verified")
        return status

    def start(self, *, timeout: float = _DEFAULT_START_TIMEOUT) -> BridgeProcessStatus:
        """Spawn once, then wait boundedly for this exact child to report provider readiness."""
        self._paths.ensure_private_runtime()
        start_lock = _FileLock(self._paths.bridge_start_lock)
        start_lock.acquire_blocking()
        try:
            existing = self.status()
            if existing.running:
                ready, last = self._wait_ready(
                    expected_pid=existing.pid,
                    expected_token=existing.token,
                    timeout=timeout,
                )
                if ready is not None:
                    return ready
                detail = last.detail or "the existing bridge did not report a ready provider"
                raise BridgeStartupError(
                    f"Régie bridge did not become ready within {timeout:g}s: {detail}"
                )
            token = secrets.token_urlsafe(24)
            process = self._spawn_worker(token)
            ready, last = self._wait_ready(
                expected_pid=process.pid,
                expected_token=token,
                timeout=timeout,
                process=process,
            )
            if ready is not None:
                return ready
            self._terminate_started(process)
            detail = last.detail or "bridge did not report a ready provider"
            raise BridgeStartupError(
                f"Régie bridge did not become ready within {timeout:g}s: {detail}"
            )
        finally:
            start_lock.release()

    def stop(self, *, timeout: float = _DEFAULT_START_TIMEOUT) -> BridgeProcessStatus:
        """Ask the proven bridge PID to exit; never signal a terminal or a daemon."""
        current = self.status()
        if not current.running or current.pid is None:
            return current
        try:
            os.kill(current.pid, signal.SIGTERM)
        except ProcessLookupError:
            return BridgeProcessStatus.stopped()
        deadline = self._monotonic() + timeout
        while _pid_alive(current.pid) and self._monotonic() < deadline:
            self._sleep(_POLL_SECONDS)
        if _pid_alive(current.pid):
            return BridgeProcessStatus(
                running=True,
                connection_state=current.connection_state,
                pid=current.pid,
                provider_id=current.provider_id,
                provider_generation=current.provider_generation,
                tmux_server_identity=current.tmux_server_identity,
                detail="bridge did not exit after SIGTERM; preserving it rather than escalating",
                token=current.token,
            )
        stopped = BridgeProcessStatus.stopped()
        _write_status(self._paths.bridge_status_path, stopped)
        self._paths.bridge_pid_path.unlink(missing_ok=True)
        return stopped

    def _spawn_worker(self, token: str) -> _Popen:
        descriptor = os.open(
            self._paths.bridge_log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
        )
        output = os.fdopen(descriptor, "ab", closefd=True)
        environment = os.environ.copy()
        # Keep TMUX so the bridge remains pinned to the invoking server, but do
        # not retain a UI pane as its implicit command target after that pane exits.
        environment.pop("TMUX_PANE", None)
        try:
            return self._popen(
                [
                    sys.executable,
                    "-m",
                    _BRIDGE_WORKER_MODULE,
                    "_bridge-worker",
                    "--home",
                    str(self._paths.theater_home),
                    "--socket",
                    str(self._socket_path),
                    "--selector",
                    self._selector,
                    "--client-id",
                    self._client_id,
                    "--token",
                    token,
                ],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                start_new_session=True,
                env=environment,
            )
        finally:
            output.close()

    @staticmethod
    def _terminate_started(process: _Popen) -> None:
        if process.poll() is None:
            process.terminate()

    def _wait_ready(
        self,
        *,
        expected_pid: int | None,
        expected_token: str | None,
        timeout: float,
        process: _Popen | None = None,
    ) -> tuple[BridgeProcessStatus | None, BridgeProcessStatus]:
        deadline = self._monotonic() + timeout
        last = BridgeProcessStatus.stopped()
        while self._monotonic() < deadline:
            current = self.status()
            if current.pid == expected_pid and (
                expected_token is None or current.token == expected_token
            ):
                last = current
                if current.connection_state == "online":
                    return current, last
                if current.connection_state == "failed":
                    break
            if process is not None and process.poll() is not None:
                break
            self._sleep(_POLL_SECONDS)
        return None, last


async def run_bridge_worker(
    *,
    paths: RegiePaths,
    socket_path: Path,
    selector: str,
    client_id: str,
    token: str,
) -> int:
    """Run one bridge child and publish only its own bounded status facts."""
    from regie.bridge.runtime import TmuxBridge

    paths.ensure_private_runtime()
    from regie.observability import configure_bridge_logging

    configure_bridge_logging()
    lease = _FileLock(paths.bridge_process_lock)
    try:
        lease.acquire()
    except BridgeStartupError as exc:
        _write_status(paths.bridge_status_path, BridgeProcessStatus.stopped(str(exc)))
        return 1
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_value in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signal_value, stop.set)
    bridge = TmuxBridge(
        BridgeConfig(
            theater_socket=socket_path,
            state_dir=paths.bridge_state_dir,
            selector=selector,
            client_id=client_id,
        )
    )
    _write_pid(paths.bridge_pid_path, os.getpid(), token)
    _write_status(
        paths.bridge_status_path,
        BridgeProcessStatus(
            running=True, connection_state="starting", pid=os.getpid(), token=token
        ),
    )
    task = asyncio.create_task(bridge.run())
    exit_code = 0
    try:
        while not task.done() and not stop.is_set():
            _write_status(paths.bridge_status_path, _bridge_status(bridge.status, token))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=0.1)
        if stop.is_set():
            await bridge.close()
        await task
    except Exception as exc:
        exit_code = 1
        _write_status(
            paths.bridge_status_path,
            BridgeProcessStatus(
                running=True,
                connection_state="failed",
                pid=os.getpid(),
                detail=f"{type(exc).__name__}: {exc}"[:1024],
                token=token,
            ),
        )
    finally:
        if not task.done():
            await bridge.close()
            await task
        _write_status(paths.bridge_status_path, BridgeProcessStatus.stopped())
        paths.bridge_pid_path.unlink(missing_ok=True)
        lease.release()
    return exit_code


def _bridge_status(status: BridgeStatus, token: str) -> BridgeProcessStatus:
    return BridgeProcessStatus(
        running=status.running,
        connection_state=status.connection_state,
        pid=os.getpid(),
        provider_id=status.provider_id,
        provider_generation=status.provider_generation,
        tmux_server_identity=status.tmux_server_identity,
        detail=status.detail,
        token=token,
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lock_held(path: Path) -> bool:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    handle = os.fdopen(descriptor, "r", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


def _bridge_worker_matches(pid: int, token: str) -> bool:
    """Fail closed unless the PID is this launcher's exact worker command."""
    try:
        result = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            check=True,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
        arguments = shlex.split(result.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    try:
        module = arguments[arguments.index("-m") + 1]
        worker = arguments[arguments.index("_bridge-worker")]
        worker_token = arguments[arguments.index("--token") + 1]
    except (IndexError, ValueError):
        return False
    return module == _BRIDGE_WORKER_MODULE and worker == "_bridge-worker" and worker_token == token


def _read_status(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return {"detail": "bridge status file is unreadable"}
    return value if isinstance(value, dict) else {"detail": "bridge status file is invalid"}


def _status_from_wire(value: dict[str, object]) -> BridgeProcessStatus:
    pid = value.get("pid")
    generation = value.get("provider_generation")
    provider_id = value.get("provider_id")
    tmux_server_identity = value.get("tmux_server_identity")
    detail = value.get("detail")
    token = value.get("token")
    return BridgeProcessStatus(
        running=value.get("running") is True,
        connection_state=str(value.get("connection_state", "unknown")),
        pid=pid if type(pid) is int and pid > 0 else None,
        provider_id=provider_id if isinstance(provider_id, str) else None,
        provider_generation=generation if type(generation) is int else None,
        tmux_server_identity=tmux_server_identity
        if isinstance(tmux_server_identity, str)
        else None,
        detail=detail if isinstance(detail, str) else None,
        token=token if isinstance(token, str) else None,
    )


def _read_pid(path: Path) -> tuple[int, str] | None:
    try:
        raw = path.read_text(encoding="ascii").strip().split()
    except OSError:
        return None
    if len(raw) != 2:
        return None
    try:
        pid = int(raw[0])
    except ValueError:
        return None
    return (pid, raw[1]) if pid > 0 and raw[1] else None


def _write_pid(path: Path, pid: int, token: str) -> None:
    _atomic_write(path, f"{pid} {token}\n".encode("ascii"))


def _write_status(path: Path, status: BridgeProcessStatus) -> None:
    _atomic_write(
        path,
        (json.dumps(asdict(status), sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
    )


def _atomic_write(path: Path, contents: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = [
    "BridgeProcessManager",
    "BridgeProcessStatus",
    "BridgeStartupError",
    "IncompatibleDaemon",
    "RegieStartupError",
    "connect_or_start_daemon",
    "run_bridge_worker",
]
