"""Filesystem change notifications (kqueue) that only mark a source dirty.

A notification never resets a semantic clock or infers absence: it sets a flag and wakes the
source, which still re-reads state itself. Polling stays the safety net and the full fallback.
Imports nothing from ``theater.daemon``; the notification path performs no store work.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import os
import select
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger("theater.harness.filesystem")


def _kqueue_module() -> Any:
    """kqueue attributes exist only on BSD/macOS; stubs follow the checking platform."""
    return cast("Any", select)


#: Descriptors the process-wide watcher may hold; beyond this a source simply polls.
MAX_WATCH_FDS = 512
#: A watched-and-quiet source still does one real check this often (missed-event safety net).
FALLBACK_CHECK_SECONDS = 5.0
#: After a watch fails or cannot be armed, polling runs this long before another attempt.
RETRY_AFTER_FAILURE_SECONDS = 30.0
_EVENTS_PER_PUMP = 64
#: macOS open-for-notifications-only flag; it never blocks an unmount.
_O_EVTONLY = getattr(os, "O_EVTONLY", 0x8000 if sys.platform == "darwin" else os.O_RDONLY)

if hasattr(select, "kqueue"):
    _kq = _kqueue_module()
    _FILE_FFLAGS = (
        _kq.KQ_NOTE_WRITE
        | _kq.KQ_NOTE_EXTEND
        | _kq.KQ_NOTE_ATTRIB
        | _kq.KQ_NOTE_DELETE
        | _kq.KQ_NOTE_RENAME
        | _kq.KQ_NOTE_REVOKE
        | _kq.KQ_NOTE_LINK
    )
    _DIR_FFLAGS = _kq.KQ_NOTE_WRITE | _kq.KQ_NOTE_DELETE | _kq.KQ_NOTE_RENAME | _kq.KQ_NOTE_REVOKE
    #: The watched inode is gone or replaced; the descriptor must be reopened by path.
    _REPLACED = _kq.KQ_NOTE_DELETE | _kq.KQ_NOTE_RENAME | _kq.KQ_NOTE_REVOKE | _kq.KQ_NOTE_LINK
else:  # pragma: no cover - non-kqueue platforms
    _FILE_FFLAGS = _DIR_FFLAGS = _REPLACED = 0


def kqueue_available() -> bool:
    return hasattr(select, "kqueue")


class WatchSubscription:
    """One source's interest in one path: a dirty flag plus failure state."""

    def __init__(
        self,
        watcher: FilesystemWatcher,
        path: Path,
        wake: Callable[[], None] | None,
        fallback_seconds: float,
    ) -> None:
        self._watcher = watcher
        self.loop = watcher._loop
        self.path = path
        self._wake = wake
        self._fallback = fallback_seconds
        # Dirty from birth: a change before registration completed must not be skipped.
        self.dirty = True
        self.failed = False
        self.closed = False
        self._checked_at = 0.0

    @property
    def active(self) -> bool:
        return not self.failed and not self.closed

    def due(self) -> bool:
        """Clear-before-read: True when the caller must do a real check now."""
        if not self.active:
            return True
        self._watcher.pump()
        now = time.monotonic()
        if self.dirty or not self.active or now - self._checked_at >= self._fallback:
            self.dirty = False
            self._checked_at = now
            return True
        return False

    def set_wake(self, wake: Callable[[], None] | None) -> None:
        self._wake = wake

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._watcher.release(self)

    def _notify(self) -> None:
        self.dirty = True
        if self._wake is not None:
            with contextlib.suppress(Exception):
                self._wake()

    def _fail(self) -> None:
        self.failed = True
        self._notify()


@dataclass(slots=True)
class _Entry:
    path: Path
    is_dir: bool
    fd: int = -1
    subs: set[WatchSubscription] = field(default_factory=set)


class FilesystemWatcher:
    """One kqueue per event loop; watches are deduplicated per file and per parent directory."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._kq: Any | None = _kqueue_module().kqueue()
        self._entries: dict[tuple[bool, Path], _Entry] = {}
        self._by_fd: dict[int, _Entry] = {}
        loop.add_reader(self._kq.fileno(), self.pump)

    @property
    def closed(self) -> bool:
        return self._kq is None

    def watch(
        self,
        path: Path,
        *,
        wake: Callable[[], None] | None = None,
        fallback_seconds: float = FALLBACK_CHECK_SECONDS,
    ) -> WatchSubscription | None:
        """Watch *path* and its parent directory; ``None`` means the caller must poll."""
        if self._kq is None:
            return None
        path = Path(path).absolute()
        sub = WatchSubscription(self, path, wake, fallback_seconds)
        dir_entry = self._acquire(path.parent, is_dir=True)
        if dir_entry is None:
            return None
        file_entry = self._acquire(path, is_dir=False)
        if file_entry is None:
            if not dir_entry.subs:
                self._drop(dir_entry)
            if not self._entries:
                self.close()
            return None
        dir_entry.subs.add(sub)
        file_entry.subs.add(sub)
        return sub

    def release(self, sub: WatchSubscription) -> None:
        for key in ((True, sub.path.parent), (False, sub.path)):
            entry = self._entries.get(key)
            if entry is not None:
                entry.subs.discard(sub)
                if not entry.subs:
                    self._drop(entry)
        if not self._entries:
            self.close()

    def pump(self) -> None:
        """Drain pending kernel events without blocking; safe from the reader or a poll."""
        kq = self._kq
        if kq is None:
            return
        try:
            while True:
                events = kq.control(None, _EVENTS_PER_PUMP, 0)
                for event in events:
                    self._dispatch(event)
                if len(events) < _EVENTS_PER_PUMP or self._kq is None:
                    return
        except OSError:
            logger.warning("kqueue wait failed; sources revert to polling", exc_info=True)
            self.close()

    def close(self) -> None:
        """Remove the reader, close every descriptor, and send every holder back to polling."""
        kq, self._kq = self._kq, None
        if kq is None:
            return
        if _WATCHERS.get(self._loop) is self:
            del _WATCHERS[self._loop]
        with contextlib.suppress(Exception):
            self._loop.remove_reader(kq.fileno())
        entries = list(self._entries.values())
        self._entries.clear()
        self._by_fd.clear()
        for entry in entries:
            with contextlib.suppress(OSError):
                os.close(entry.fd)
        kq.close()
        for sub in {sub for entry in entries for sub in entry.subs}:
            sub._fail()

    # ---- internals ------------------------------------------------------

    @staticmethod
    def _exists(path: Path) -> bool:
        try:
            path.stat()
        except OSError as exc:
            return exc.errno != errno.ENOENT
        return True

    def _acquire(self, path: Path, *, is_dir: bool) -> _Entry | None:
        key = (is_dir, path)
        entry = self._entries.get(key)
        if entry is not None:
            return entry
        if len(self._entries) >= MAX_WATCH_FDS:
            return None
        entry = _Entry(path=path, is_dir=is_dir)
        # An absent file stays tracked descriptor-less; the directory watch reports its creation.
        if not self._arm(entry) and (is_dir or self._exists(path)):
            return None
        self._entries[key] = entry
        return entry

    def _arm(self, entry: _Entry) -> bool:
        assert self._kq is not None
        try:
            fd = os.open(entry.path, _O_EVTONLY)
        except OSError:
            return False
        _kq = _kqueue_module()
        flags = _kq.KQ_EV_ADD | _kq.KQ_EV_ENABLE | _kq.KQ_EV_CLEAR
        fflags = _DIR_FFLAGS if entry.is_dir else _FILE_FFLAGS
        try:
            self._kq.control([_kq.kevent(fd, _kq.KQ_FILTER_VNODE, flags, fflags)], 0, 0)
        except OSError:
            os.close(fd)
            return False
        entry.fd = fd
        self._by_fd[fd] = entry
        return True

    def _drop(self, entry: _Entry) -> None:
        self._entries.pop((entry.is_dir, entry.path), None)
        self._by_fd.pop(entry.fd, None)
        if entry.fd >= 0:
            with contextlib.suppress(OSError):
                os.close(entry.fd)  # closing the descriptor also removes its kevent
            entry.fd = -1

    def _dispatch(self, event: Any) -> None:
        entry = self._by_fd.get(event.ident)
        if entry is None:
            return
        subs = tuple(entry.subs)
        if event.flags & _kqueue_module().KQ_EV_ERROR:
            self._fail_entry(entry)
            return
        for sub in subs:
            sub._notify()
        if event.fflags & _REPLACED:
            self._rearm(entry)
        if entry.is_dir:
            self._arm_returned_files(subs)

    def _arm_returned_files(self, subs: tuple[WatchSubscription, ...]) -> None:
        """A directory change may have created a watched file that had no descriptor yet."""
        for sub in subs:
            file_entry = self._entries.get((False, sub.path))
            if (
                file_entry is not None
                and file_entry.fd == -1
                and self._kq is not None
                and not self._arm(file_entry)
                and self._exists(file_entry.path)
            ):
                self._fail_entry(file_entry)

    def _rearm(self, entry: _Entry) -> None:
        """The inode was replaced or removed: follow the path to whatever is there now."""
        self._by_fd.pop(entry.fd, None)
        with contextlib.suppress(OSError):
            os.close(entry.fd)
        entry.fd = -1
        try:
            entry.path.stat()
        except OSError as exc:
            if exc.errno == errno.ENOENT and not entry.is_dir:
                return  # the directory watch reports the file's return
            self._fail_entry(entry)
            return
        if not self._arm(entry):
            self._fail_entry(entry)

    def _fail_entry(self, entry: _Entry) -> None:
        subs = tuple(entry.subs)
        self._drop(entry)
        entry.subs.clear()
        for sub in subs:
            sub._fail()
        if not self._entries:
            self.close()


_WATCHERS: dict[asyncio.AbstractEventLoop, FilesystemWatcher] = {}


def shared_watcher() -> FilesystemWatcher | None:
    """The running loop's watcher (one per loop), created lazily; ``None`` without kqueue."""
    if not kqueue_available():
        return None
    loop = asyncio.get_running_loop()
    for dead in [other for other in _WATCHERS if other.is_closed()]:
        _WATCHERS[dead].close()
    watcher = _WATCHERS.get(loop)
    if watcher is None or watcher.closed:
        try:
            watcher = _WATCHERS[loop] = FilesystemWatcher(loop)
        except OSError:
            return None
    return watcher


class WatchGate:
    """Per-source helper: keeps one subscription on the current path and gates real checks."""

    def __init__(
        self,
        *,
        fallback_seconds: float = FALLBACK_CHECK_SECONDS,
        wake: Callable[[], None] | None = None,
    ) -> None:
        self._fallback = fallback_seconds
        self._wake = wake
        self._sub: WatchSubscription | None = None
        self._retry_at = 0.0

    def set_wake(self, wake: Callable[[], None] | None) -> None:
        self._wake = wake
        if self._sub is not None:
            self._sub.set_wake(wake)

    @property
    def active(self) -> bool:
        return self._sub is not None and self._sub.active

    def due(self, path: Path) -> bool:
        """True when *path* must be checked for real; always True without a live watch."""
        sub = self._sub
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            return True
        if sub is not None and sub.loop is not running:
            self.release()  # migrated to another loop: its watcher belongs to the old one
            sub = None
        if sub is not None and (sub.path != Path(path).absolute() or not sub.active):
            if not sub.active and not sub.closed:
                self._retry_at = time.monotonic() + RETRY_AFTER_FAILURE_SECONDS
            sub.close()
            sub = self._sub = None
        if sub is None and time.monotonic() >= self._retry_at:
            watcher = shared_watcher()
            if watcher is not None:
                sub = self._sub = watcher.watch(
                    path, wake=self._wake, fallback_seconds=self._fallback
                )
            if sub is None:
                self._retry_at = time.monotonic() + RETRY_AFTER_FAILURE_SECONDS
        return True if sub is None else sub.due()

    def release(self) -> None:
        sub, self._sub = self._sub, None
        if sub is not None:
            sub.close()


__all__ = [
    "FALLBACK_CHECK_SECONDS",
    "FilesystemWatcher",
    "WatchGate",
    "WatchSubscription",
    "kqueue_available",
    "shared_watcher",
]
