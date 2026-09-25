"""What the OS says about a process: its descendants, and the files it holds open.
Shells out (``ps``, ``lsof``) rather than depend on a C-extension wheel; every function answers
"nothing" instead of raising, since vanished processes are normal.
"""

from __future__ import annotations

import logging
import subprocess
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from theater import timing
from theater.observability.catalog import PROC_LSOF, PROC_PS_COMM, PROC_PS_TABLE

logger = logging.getLogger("theater.proc")

#: Both probes are read-only kernel interrogations; timeout guards a wedged-network-mount lsof.
_TIMEOUT = 5

#: lsof -F prefixes file names with this; a name is every line after an 'n'.
_LSOF_NAME = "n"


@dataclass(frozen=True, slots=True)
class ProcessSnapshot:
    """One parsed ``ps`` table, reusable across many ``descendants()`` calls.

    Only ``capture()`` shells out, so multi-pid sweeps pay for one ``ps``.
    """

    _children: dict[int, list[tuple[int, str]]] = field(default_factory=dict)
    _comms: dict[int, str] = field(default_factory=dict)

    @classmethod
    def capture(cls) -> ProcessSnapshot:
        """Parse the whole machine's process table exactly once."""
        children, comms = _process_table()
        return cls(_children=children, _comms=comms)

    def descendants(self, root_pid: int) -> list[tuple[int, str]]:
        """`(pid, comm)` for every descendant of *root_pid* in this snapshot, breadth-first.

        The root itself is excluded — callers that care about it already have it.
        """
        found: list[tuple[int, str]] = []
        queue = deque([root_pid])
        seen = {root_pid}
        while queue:
            pid = queue.popleft()
            for child_pid, comm in self._children.get(pid, []):
                if child_pid in seen:
                    # Cycle impossible in a real process table, but a loop here hangs the daemon.
                    continue
                seen.add(child_pid)
                found.append((child_pid, comm))
                queue.append(child_pid)
        return found

    def comm(self, pid: int) -> str:
        """The command name from this snapshot, or "" if unknown; never shells out."""
        return self._comms.get(pid, "")


def descendants(root_pid: int) -> list[tuple[int, str]]:
    """``(pid, comm)`` for every descendant of *root_pid*, breadth-first, from a fresh snapshot.

    For several pids, capture one ``ProcessSnapshot`` and reuse it.
    """
    return ProcessSnapshot.capture().descendants(root_pid)


def comm(pid: int) -> str:
    """The command name of one process via one ``ps``, or "" if there is no such process."""
    return _comm(pid)


def open_files(pid: int) -> list[Path]:
    """Absolute paths of the files *pid* holds open, via ``/proc`` or ``lsof``.

    Best effort: an empty list means "no evidence", never "no files".
    """
    fds = Path("/proc") / str(pid) / "fd"
    if fds.is_dir():
        return _proc_open_files(fds)
    return _lsof_open_files(pid)


# ---- internals ----------------------------------------------------------


def _process_table() -> tuple[dict[int, list[tuple[int, str]]], dict[int, str]]:
    """Parent pid → children ``(pid, comm)`` and pid → comm, parsed from one ``ps`` pass."""
    try:
        with timing.span(PROC_PS_TABLE):
            out = subprocess.check_output(
                ["ps", "-eo", "pid,ppid,comm"],
                text=True,
                timeout=_TIMEOUT,
            )
    except (OSError, subprocess.SubprocessError):
        return {}, {}
    children: dict[int, list[tuple[int, str]]] = {}
    comms: dict[int, str] = {}
    for line in out.strip().splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        comm = parts[2]
        children.setdefault(ppid, []).append((pid, comm))
        comms[pid] = comm
    return children, comms


def _comm(pid: int) -> str:
    """The command name of one process, or the empty string if it is gone."""
    try:
        with timing.span(PROC_PS_COMM, pid=pid):
            try:
                out = subprocess.check_output(
                    ["ps", "-p", str(pid), "-o", "comm="],
                    text=True,
                    stderr=subprocess.PIPE,
                    timeout=_TIMEOUT,
                )
            except subprocess.CalledProcessError as exc:
                # A successful ps query with no matches exits 1 without diagnostics.
                if exc.returncode == 1 and not exc.output and not exc.stderr:
                    return ""
                raise
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.strip()


def _proc_open_files(fds: Path) -> list[Path]:
    found: list[Path] = []
    try:
        entries = list(fds.iterdir())
    except OSError:
        # Another user's process, or one that exited mid-scan.
        return []
    for entry in entries:
        try:
            target = str(entry.readlink())
        except OSError:
            continue
        if not target.startswith("/"):
            # Sockets, pipes, epoll handles read back as socket:[12345], not a path.
            continue
        if target.endswith(" (deleted)"):
            # Inode held but name gone; a correlation on it would point at nothing.
            continue
        found.append(Path(target))
    return found


def _lsof_open_files(pid: int) -> list[Path]:
    """Parse ``lsof -F n`` (one letter-prefixed field per line).
    ``-n -P`` skip slow name resolution; the exit status is ignored since partial failure is
    routine.
    """
    try:
        with timing.span(PROC_LSOF, pid=pid):
            completed = subprocess.run(
                ["lsof", "-n", "-P", "-p", str(pid), "-F", "n"],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
                check=False,
            )
    except (OSError, subprocess.SubprocessError):
        return []
    found: list[Path] = []
    for line in completed.stdout.splitlines():
        if not line.startswith(_LSOF_NAME):
            continue
        name = line[1:]
        # Sockets and pipes are named too (->127.0.0.1:443, pipe); leading slash separates files.
        if name.startswith("/"):
            found.append(Path(name))
    return found
