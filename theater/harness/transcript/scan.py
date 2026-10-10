"""One directory walk per transcript domain: single-stat entries, overlapping scans coalesced."""

from __future__ import annotations

import os
import stat
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ScanEntry:
    """A matched path with the one ``lstat`` its scan took (``None`` when unreadable)."""

    path: Path
    is_symlink: bool
    st: os.stat_result | None

    def followed_stat(self) -> os.stat_result | None:
        """The symlink-following stat; only a symlink costs a second syscall."""
        if self.st is None or not self.is_symlink:
            return self.st
        try:
            return self.path.stat()
        except OSError:
            return None


class _Flight:
    __slots__ = ("done", "entries", "waiters")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.entries: tuple[ScanEntry, ...] | None = None
        self.waiters = 0


_lock = threading.Lock()
_flights: dict[tuple[str, str], _Flight] = {}


def scan_domain(root: Path, pattern: str) -> tuple[tuple[ScanEntry, ...], bool]:
    """Walk ``root`` for ``pattern``; the flag is True when an in-flight walk was joined.

    Only a walk still running is shared: a call starting after it finished walks afresh, so
    sequential observations (identity-loss confirmations) never reuse one another's results.
    """
    key = (str(root), pattern)
    while True:
        with _lock:
            flight = _flights.get(key)
            leader = flight is None
            if flight is None:
                flight = _flights[key] = _Flight()
            else:
                flight.waiters += 1
        if leader:
            break
        flight.done.wait()
        if flight.entries is not None:
            return flight.entries, True
    try:
        flight.entries = tuple(_walk(root, pattern))
        return flight.entries, False
    finally:
        with _lock:
            _flights.pop(key, None)
        flight.done.set()


def _lstat(entry: os.DirEntry[str]) -> os.stat_result | None:
    try:
        return entry.stat(follow_symlinks=False)
    except OSError:
        return None


def _walk(root: Path, pattern: str) -> Iterator[ScanEntry]:
    segments = pattern.split("/")
    if "**" in pattern or not all(segments):
        yield from _glob_walk(root, pattern)
        return
    yield from _descend(root, segments)


def _descend(directory: Path, segments: list[str]) -> Iterator[ScanEntry]:
    try:
        with os.scandir(directory) as it:
            entries = list(it)
    except OSError:
        return
    head, rest = segments[0], segments[1:]
    for entry in entries:
        if not fnmatchcase(entry.name, head):
            continue
        path = directory / entry.name
        if not rest:
            yield ScanEntry(path, entry.is_symlink(), _lstat(entry))
            continue
        try:
            is_dir = entry.is_dir()
        except OSError:
            continue
        if is_dir:
            yield from _descend(path, rest)


def _glob_walk(root: Path, pattern: str) -> Iterator[ScanEntry]:
    for path in root.glob(pattern):
        try:
            st = os.lstat(path)
        except OSError:
            yield ScanEntry(path, False, None)
            continue
        yield ScanEntry(path, stat.S_ISLNK(st.st_mode), st)
