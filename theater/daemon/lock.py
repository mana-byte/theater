"""Who is the daemon: an flock'd pidfile, not a socket's presence on disk.
Probe-unlink-bind let a second daemon delete the first's fresh socket. flock is atomic, dropped by
the kernel on SIGKILL, and per open file description; the pid inside is only a human diagnostic.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import logging
import os
import subprocess
from pathlib import Path

from theater import paths

logger = logging.getLogger("theater.daemon")

#: errnos meaning "someone else holds it"; EWOULDBLOCK/EAGAIN on Linux+macOS, EACCES on others.
_HELD = frozenset({errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES})


class LockHeld(RuntimeError):
    """Another daemon holds the lock.

    A RuntimeError so ``cmd_daemon`` reports it as one line: this is normal, not a crash.
    """

    def __init__(self, pid: int | None) -> None:
        self.pid = pid
        who = f"pid {pid}" if pid else "pid unknown"
        super().__init__(f"a theater daemon is already running ({who})")


def file_id(path: Path) -> tuple[int, int] | None:
    """(device, inode) for a path, or None if it is not there.
    Identity, not existence: deleting only our own inode stops one daemon destroying another's
    files.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def read_pid(path: Path | None = None) -> int | None:
    """The pid recorded in the file, if it holds a plausible one."""
    target = path or paths.pidfile_path()
    try:
        return int(target.read_text().strip())
    except (OSError, ValueError):
        return None


def _live_daemon_pid(path: Path) -> int | None:
    """The pid recorded in ``path``, if a running theater process still owns it.
    Fallback only where flock is unavailable; ``ps`` matches the command so a recycled pid is not a
    daemon. Every failure answers None so a degraded machine can still start one.
    """
    pid = read_pid(path)
    if pid is None:
        return None
    try:
        probe = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if probe.returncode != 0:
        return None
    return pid if "theater" in probe.stdout else None


def is_free(path: Path | None = None) -> bool:
    """True when no live daemon holds the lock.
    Opens without O_CREAT so asking leaves no file behind; falls back to the recorded pid without
    flock.
    """
    target = path or paths.pidfile_path()
    try:
        fd = os.open(target, os.O_RDWR)
    except OSError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in _HELD:
            return False
        return _live_daemon_pid(target) is None
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


class DaemonLock:
    """The right to be the daemon, held for as long as the fd is open."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or paths.pidfile_path()
        self._fd: int | None = None
        #: False when the filesystem cannot lock — see acquire().
        self.enforced = True

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        """Take the lock, or raise LockHeld naming who has it.

        No O_TRUNC: truncating before winning would erase the holder's pid from our error.
        """
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in _HELD:
                pid = read_pid(self.path)
                os.close(fd)
                raise LockHeld(pid) from exc
            # NFS/FUSE may lack flock; carry on unlocked — pid file still rules out the common case.
            live = _live_daemon_pid(self.path)
            if live is not None:
                os.close(fd)
                raise LockHeld(live) from exc
            logger.warning("cannot lock %s (%s); singleton enforcement is off", self.path, exc)
            self.enforced = False
        self._fd = fd
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)

    def release(self) -> None:
        """Drop the lock and remove the pidfile, if it is still ours; idempotent.

        Unlink is inode-guarded so a slowly dying daemon never deletes its replacement's pidfile.
        """
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        if file_id(self.path) == _fd_id(fd):
            with contextlib.suppress(OSError):
                self.path.unlink()
        # Closing releases the flock; last so the file is gone before anyone waiting sees it free.
        with contextlib.suppress(OSError):
            os.close(fd)


def _fd_id(fd: int) -> tuple[int, int] | None:
    try:
        st = os.fstat(fd)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)
