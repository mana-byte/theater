"""Run subprocesses in their own session and reap everything they started on every exit path.

Ownership is a random token in the child's environment; a command line naming a path proves nothing.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import IO, Literal, overload

from theater.constants.harness import HARNESS_COMPAT_SANDBOX_ENV as SANDBOX_ENV  # noqa: F401

from ..contract import CleanupFailed

RUN_ENV = "HARNESS_COMPAT_RUN"
_GRACE_SECONDS = 5.0
_INTERRUPT_SECONDS = 3.0
_POLL_SECONDS = 0.1


def new_token() -> str:
    return uuid.uuid4().hex


def _ps(columns: str, *flags: str) -> str:
    try:
        with subprocess.Popen(
            ["ps", *flags, "-A", "-ww", "-o", columns],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ) as ps:
            return ps.communicate(timeout=20)[0]
    except (OSError, subprocess.SubprocessError):
        return ""


def process_table() -> dict[int, tuple[int, str]]:
    """pid -> (parent pid, command line) for every process ``ps`` shows; {} if it is missing."""
    table: dict[int, tuple[int, str]] = {}
    for line in _ps("pid=,ppid=,command=").splitlines():
        fields = line.split(None, 2)
        if len(fields) >= 2 and fields[0].isdigit() and fields[1].isdigit():
            table[int(fields[0])] = (int(fields[1]), fields[2] if len(fields) > 2 else "")
    return table


def descendants(pid: int, table: Mapping[int, tuple[int, str]]) -> set[int]:
    """Every process below ``pid`` in the snapshot, whatever session or group it moved to."""
    children: dict[int, list[int]] = {}
    for child, (parent, _) in table.items():
        children.setdefault(parent, []).append(child)
    found: set[int] = set()
    stack = [pid]
    while stack:
        for child in children.get(stack.pop(), ()):
            if child not in found:
                found.add(child)
                stack.append(child)
    return found


def ancestry(pid: int, table: Mapping[int, tuple[int, str]]) -> set[int]:
    """``pid`` and its parents: processes a cleanup must never signal."""
    chain = {pid}
    while pid in table and table[pid][0] not in chain and table[pid][0] > 0:
        pid = table[pid][0]
        chain.add(pid)
    return chain


def _procargs2(pid: int) -> bytes:
    """The kernel's argc, executable, argv and environment block of ``pid`` (macOS)."""
    import ctypes

    libc = ctypes.CDLL(None)
    libc.sysctl.argtypes = [
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2, pid
    size = ctypes.c_size_t(0)
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) or not size.value:
        return b""
    buffer = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0):
        return b""
    return buffer.raw[: size.value]


def _environ_entries(pid: int) -> list[bytes]:
    """The exact ``NAME=value`` entries of ``pid``'s environment; [] when it cannot be read."""
    environ = Path("/proc") / str(pid) / "environ"
    if Path("/proc/self/environ").exists():
        with contextlib.suppress(OSError):
            return environ.read_bytes().split(b"\0")
        return []
    data = _procargs2(pid)
    if len(data) < 4:
        return []
    argc = int.from_bytes(data[:4], sys.byteorder)
    rest = data[4:]
    fields = rest[rest.find(b"\0") + 1 :].lstrip(b"\0").split(b"\0")  # skip executable, padding
    return [entry for entry in fields[argc:] if entry]


def owned_pids(name: str, token: str) -> set[int]:
    """Processes whose environment holds the exact entry ``name=token``, wherever they moved."""
    needle = f"{name}={token}".encode()
    if Path("/proc/self/environ").exists():
        pids = [int(entry.name) for entry in Path("/proc").iterdir() if entry.name.isdigit()]
    else:
        pids = list(process_table())
    return {pid for pid in pids if needle in _environ_entries(pid)}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _live_pids(pids: Iterable[int]) -> set[int]:
    # Zombies cannot write files and must be reaped by their own parent.
    states: dict[int, str] = {}
    for line in _ps("pid=,stat=").splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0].isdigit():
            states[int(fields[0])] = fields[1]
    return {pid for pid in pids if _alive(pid) and not states.get(pid, "").startswith("Z")}


def _wait_exit(pids: set[int], seconds: float) -> set[int]:
    deadline = time.monotonic() + seconds
    while live := _live_pids(pids):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return live
        time.sleep(min(_POLL_SECONDS, remaining))
    return set()


def kill_all(pids: Iterable[int], *, grace: float = _GRACE_SECONDS) -> None:
    """SIGTERM then SIGKILL survivors; return only once they cannot mutate files."""
    live = _live_pids(pid for pid in pids if pid != os.getpid())
    for pid in live:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
    live = _wait_exit(live, grace)
    for pid in live:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
    if remaining := _wait_exit(live, _GRACE_SECONDS):
        raise CleanupFailed(f"process cleanup failed: PIDs {sorted(remaining)} survived SIGKILL")


def stop_owned(name: str, token: str) -> None:
    """Stop owned processes, including late children; never signal this process or its parents."""
    protected = ancestry(os.getpid(), process_table())
    deadline = time.monotonic() + 2 * _GRACE_SECONDS
    while victims := _live_pids(owned_pids(name, token) - protected):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CleanupFailed(f"process cleanup failed: owned PIDs {sorted(victims)} remain")
        kill_all(victims, grace=min(_GRACE_SECONDS, remaining))


@overload
def spawn_group(
    command: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path | None = None,
    stdin: int | None = None,
    stdout: int | IO[str] | None = None,
    stderr: int | IO[str] | None = None,
    text: Literal[True],
) -> subprocess.Popen[str]: ...


@overload
def spawn_group(
    command: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path | None = None,
    stdin: int | None = None,
    stdout: int | IO[bytes] | None = None,
    stderr: int | IO[bytes] | None = None,
    text: Literal[False] = False,
) -> subprocess.Popen[bytes]: ...


def spawn_group(
    command: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path | None = None,
    stdin: int | None = None,
    stdout: int | IO[str] | IO[bytes] | None = None,
    stderr: int | IO[str] | IO[bytes] | None = None,
    text: bool = False,
) -> subprocess.Popen[str] | subprocess.Popen[bytes]:
    """Start a command in a private session; the caller owns its stop policy."""
    return subprocess.Popen(
        command,
        cwd=cwd,
        env=dict(env),
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        text=text,
        start_new_session=True,
    )


def run_group(
    argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    """Like subprocess.run, but every exit path reaps the child and all it started."""
    token = new_token()
    with (
        tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as out,
        tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as err,
    ):
        # Files, not pipes: a detached grandchild holding a pipe would stall a finished child.
        process = spawn_group(
            list(argv),
            cwd=cwd,
            env={**env, RUN_ENV: token},
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            text=True,
        )
        try:
            process.wait(timeout=timeout)
        except BaseException:
            tree = descendants(process.pid, process_table())
            with contextlib.suppress(ProcessLookupError, PermissionError):
                process.send_signal(signal.SIGINT)  # lets pytest fixtures stop their helpers
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=_INTERRUPT_SECONDS)
            terminate_group(process, tree)
            stop_owned(RUN_ENV, token)
            raise
        terminate_group(process)  # stragglers the child left in its own group
        stop_owned(RUN_ENV, token)  # and any it detached into another session
        out.seek(0)
        err.seek(0)
        return subprocess.CompletedProcess(list(argv), process.returncode, out.read(), err.read())


def terminate_group(process: subprocess.Popen[str], tree: Iterable[int] = ()) -> None:
    """Stop the leader's group plus ``tree`` (its snapshotted descendants); always reap it."""
    victims = set(tree)
    with contextlib.suppress(PermissionError):
        _signal_group(process.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=_GRACE_SECONDS)
    with contextlib.suppress(PermissionError):
        _signal_group(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=_GRACE_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise CleanupFailed(
            f"process cleanup failed: leader PID {process.pid} survived SIGKILL"
        ) from exc
    finally:
        kill_all(victims)


def _signal_group(pid: int, signum: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signum)
