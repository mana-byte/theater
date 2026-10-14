"""Run subprocesses in their own session and reap everything they started on every exit path.

Ownership is a random token in the child's environment; a command line naming a path proves nothing.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import tempfile
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

RUN_ENV = "HARNESS_COMPAT_RUN"
SANDBOX_ENV = "HARNESS_COMPAT_SANDBOX"
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


def owned_pids(name: str, token: str) -> set[int]:
    """Processes whose environment holds ``name=token``, wherever they were reparented to."""
    needle = f"{name}={token}"
    proc = Path("/proc")
    if (proc / "self" / "environ").exists():
        found: set[int] = set()
        for entry in proc.iterdir():
            if entry.name.isdigit():
                with contextlib.suppress(OSError):
                    if needle.encode() in (entry / "environ").read_bytes():
                        found.add(int(entry.name))
        return found
    return {
        int(line.split(None, 1)[0])
        for line in _ps("pid=,command=", "-E").splitlines()
        if needle in line and line.split(None, 1)[0].isdigit()
    }


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_all(pids: Iterable[int], *, grace: float = _GRACE_SECONDS) -> None:
    """SIGTERM every live pid, wait ``grace`` seconds, then SIGKILL the survivors."""
    live = {pid for pid in pids if pid != os.getpid() and _alive(pid)}
    for pid in live:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while live and time.monotonic() < deadline:
        time.sleep(_POLL_SECONDS)
        live = {pid for pid in live if _alive(pid)}
    for pid in live:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)


def stop_owned(name: str, token: str) -> None:
    """Kill every process started under ``name=token``; this process and its parents are exempt."""
    victims = owned_pids(name, token)
    if victims:
        kill_all(victims - ancestry(os.getpid(), process_table()))


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
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env={**env, RUN_ENV: token},
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            text=True,
            start_new_session=True,
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
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=_GRACE_SECONDS)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()
    kill_all(victims)
