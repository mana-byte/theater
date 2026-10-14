"""Run subprocesses in their own session and reap every descendant on each exit path."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

_GRACE_SECONDS = 5.0
_INTERRUPT_SECONDS = 3.0
_POLL_SECONDS = 0.1


def process_table() -> dict[int, tuple[int, str]]:
    """pid -> (parent pid, command line) for every process ``ps`` shows; {} if it is missing."""
    try:
        with subprocess.Popen(
            ["ps", "-A", "-ww", "-o", "pid=,ppid=,command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ) as ps:
            listing, _ = ps.communicate(timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {}
    table: dict[int, tuple[int, str]] = {}
    for line in listing.splitlines():
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


def run_group(
    argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    """Like subprocess.run, but every exit path reaps the child and all of its descendants."""
    with (
        tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as out,
        tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as err,
    ):
        # Files, not pipes: a detached grandchild holding a pipe would stall a finished child.
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(env),
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
            raise
        terminate_group(process)  # stragglers the child left in its own group
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
