"""The operating system's process table, read without going through Theater."""

from __future__ import annotations

import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Process:
    pid: int
    ppid: int
    pgid: int
    #: With the pid, names one process for its whole life: a recycled pid starts at another time.
    started: str


def parse(text: str) -> dict[int, Process]:
    """Rows of ``ps -o pid=,ppid=,pgid=,lstart=``; the start time is the rest of the line."""
    processes: dict[int, Process] = {}
    for line in text.splitlines():
        pid, ppid, pgid, started = line.split(None, 3)
        processes[int(pid)] = Process(int(pid), int(ppid), int(pgid), started.strip())
    return processes


def table() -> dict[int, Process]:
    shown = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,pgid=,lstart="],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return parse(shown.stdout)


def detached_children(processes: Mapping[int, Process], parent: int) -> list[Process]:
    """Children of ``parent`` leading a process group of their own: what it started detached."""
    return [p for p in processes.values() if p.ppid == parent and p.pgid == p.pid]


def lost(recorded: Iterable[Process], processes: Mapping[int, Process]) -> list[Process]:
    """The recorded processes that exited, whatever now holds their pid."""
    return [
        process
        for process in recorded
        if (now := processes.get(process.pid)) is None or now.started != process.started
    ]
