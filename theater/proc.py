"""What the OS says about a process: its descendants, and the files it holds open.
Shells out (``ps``, ``lsof``) rather than depend on a C-extension wheel; every function answers
"nothing" instead of raising, since vanished processes are normal.
"""

from __future__ import annotations

import logging
import os
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


def process_started_at(pid: int) -> float | None:
    """Strong numeric process start identity, or ``None`` when unavailable."""
    return _started_at_libproc(pid) or _started_at_linux(pid)


# ---- internals ----------------------------------------------------------


def _boot_time_linux() -> float | None:
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _started_at_linux(pid: int) -> float | None:
    """Linux boot time plus the process start ticks as epoch seconds."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    marker = raw.rfind(")")
    if marker < 0:
        return None
    fields = raw[marker + 2 :].split()
    if len(fields) < 20:
        return None
    try:
        ticks = int(fields[19])
        clock_ticks = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError):
        return None
    boot_time = _boot_time_linux()
    if boot_time is None or clock_ticks <= 0:
        return None
    return boot_time + ticks / clock_ticks


def _started_at_libproc(pid: int) -> float | None:
    """macOS process start time with microsecond resolution."""
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
        return float(info.pbi_start_tvsec) + info.pbi_start_tvusec / 1_000_000.0
    except Exception:
        return None


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
