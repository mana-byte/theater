"""What the operating system will tell us about a process.

Two questions, and nothing else belongs here. *What did this process spawn* —
asked by harness detection, because the pane's foreground command is not the
harness when `theater adopt` is the thing running. *What files does this
process hold open* — asked by transcript correlation, because a CLI that keeps
its own transcript open is telling us which transcript is its own, and that is
the only exact answer available for a harness that mints its session id
internally.

Both shell out rather than take a dependency. `psutil` would answer both more
neatly, but Theater's whole install story is that it is a `uv` script with a
tmux next to it; a wheel with a C extension in it is a worse trade than parsing
`ps` output. The same reasoning is why the daemon shells out to `tmux` instead
of speaking its control protocol.

Every function here answers "nothing" rather than raising. A process that
vanished between two calls is the normal case, not an error, and the callers
are observation paths whose whole contract is to keep watching.
"""

from __future__ import annotations

import logging
import subprocess
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from theater import timing

logger = logging.getLogger("theater.proc")

#: Both probes are read-only kernel interrogations; timeout guards a wedged-network-mount lsof.
_TIMEOUT = 5

#: lsof -F prefixes file names with this; a name is every line after an 'n'.
_LSOF_NAME = "n"


@dataclass(frozen=True, slots=True)
class ProcessSnapshot:
    """One parsed `ps` table, reusable across many `descendants()` calls.

    A caller that needs the ancestry of several pids — the daemon's unmanaged
    sweep, one candidate pane at a time — pays for one `ps` instead of one per
    pid by capturing a snapshot up front and walking it repeatedly. `capture()`
    is the only place that shells out; `descendants()` and `comm()` here never do.
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
        """The command name of one process from this snapshot, or "" if unknown.

        Unlike the module-level ``comm`` function, this reads from the already
        parsed table and never shells out. A caller that captured a snapshot
        for a descendant walk can also read root comms from it for free.
        """
        return self._comms.get(pid, "")


def descendants(root_pid: int) -> list[tuple[int, str]]:
    """`(pid, comm)` for every descendant of *root_pid*, breadth-first.

    Captures a fresh `ProcessSnapshot` for this one call. A caller that will
    ask about several pids in the same pass should capture once and call
    `ProcessSnapshot.descendants` directly instead.
    """
    return ProcessSnapshot.capture().descendants(root_pid)


def comm(pid: int) -> str:
    """The command name of one process, or "" if there is no such process.

    One `ps` for one pid, where `descendants` reads the whole table. A caller
    that only wants to know what a single known process is should ask this.
    """
    return _comm(pid)


def open_files(pid: int) -> list[Path]:
    """Absolute paths of the files *pid* holds open.

    `/proc` where there is one, `lsof` where there is not. Both are best
    effort: an unreadable `/proc/<pid>/fd` (another user's process) and a
    missing `lsof` binary both answer with an empty list, which callers must
    read as "no evidence", never as "no files".
    """
    fds = Path("/proc") / str(pid) / "fd"
    if fds.is_dir():
        return _proc_open_files(fds)
    return _lsof_open_files(pid)


# ---- internals ----------------------------------------------------------


def _process_table() -> tuple[dict[int, list[tuple[int, str]]], dict[int, str]]:
    """Parent pid → its children as `(pid, comm)`, and pid → its own comm.

    Both maps are parsed from the same ``ps`` output in one pass, each line
    contributing to both indexes. The comm string is shared by reference so
    neither index copies it.
    """
    try:
        with timing.span("proc.ps-table", slow_ms=timing.PROC_MS):
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
        with timing.span("proc.ps-comm", slow_ms=timing.PROC_MS, pid=pid):
            out = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "comm="],
                text=True,
                timeout=_TIMEOUT,
            )
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
    """`lsof -F n` output, which is one field per line prefixed by its letter.

    `-n` and `-P` suppress host and port name resolution, which is what makes
    `lsof` slow and is worthless for the file names we are after. The exit
    status is deliberately ignored: `lsof` exits non-zero when *any* file
    could not be examined, which on a normal desktop is routine, and the
    files it did examine are still on stdout.
    """
    try:
        with timing.span("proc.lsof", slow_ms=timing.PROC_MS, pid=pid):
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
