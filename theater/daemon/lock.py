"""Who is the daemon: an flock'd pidfile, not a file's presence on disk.

The old singleton test was "does daemon.sock exist, and does something answer
on it?" — probe, unlink if dead, bind. That has a window: if daemon A binds
between B's probe and B's unlink, B deletes A's fresh socket and binds its own.
A is then listening on an unlinked inode nobody can reach, and both processes
believe they are the daemon.

An advisory lock closes it because acquiring is atomic — there is no window
between testing and taking. `flock` rather than `fcntl.lockf`:

  - The kernel drops it when the fd closes, including on SIGKILL, so a stale
    lock is not a thing that can happen. A pidfile holding a number needs
    liveness checks and pid reuse handling; this needs neither.
  - It is per open file description, not per process, so two Daemon objects in
    one process conflict too. That is what makes the tests exercise the real
    constraint instead of quietly passing.

The pid is still written into the file, but as a diagnostic for humans reading
it, never as the thing consulted to decide whether a daemon is running.
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

#: errnos meaning "someone else holds it", as opposed to "locking does not work
#: on this filesystem". flock reports EWOULDBLOCK (== EAGAIN) on Linux and
#: macOS; EACCES turns up on some others.
_HELD = frozenset({errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES})


class LockHeld(RuntimeError):
    """Another daemon holds the lock.

    A RuntimeError subclass because `cmd_daemon` already turns those into a
    one-line message rather than a traceback, and "someone else is the daemon"
    is a normal thing to tell a user, not a crash.
    """

    def __init__(self, pid: int | None) -> None:
        self.pid = pid
        who = f"pid {pid}" if pid else "pid unknown"
        super().__init__(f"a theater daemon is already running ({who})")


def file_id(path: Path) -> tuple[int, int] | None:
    """(device, inode) for a path, or None if it is not there.

    Identity, not existence. Deleting by path is how one daemon destroys
    another's files; deleting only when the path still resolves to the inode we
    created is how it stops.
    """
    try:
        st = os.stat(path)
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
    """The pid recorded in `path`, if a running theater process still owns it.

    Only consulted where flock is unavailable, as a weaker stand-in for the
    guarantee the kernel would otherwise give. `ps` rather than
    `os.kill(pid, 0)`: the signal probe cannot tell the daemon from whatever
    inherited its number after a crash, so a recycled pid would read as a live
    daemon forever. Matching the command text costs one subprocess and makes
    reuse survivable.

    Every failure — no pidfile, garbage in it, `ps` missing or slow — answers
    None. This is already the degraded path; erring toward "nobody is running"
    keeps a machine that can neither lock nor run `ps` able to start a daemon
    at all, which matters more here than a guarantee that was already lost.
    """
    pid = read_pid(path)
    if pid is None:
        return None
    try:
        probe = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
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

    Used by `theater restart` to know the old daemon is really gone, and by
    `DaemonClient` to decide whether launching another daemon is worth trying.
    Opens without O_CREAT: a missing pidfile means nobody holds anything, and
    this is a question, so it must not leave a file behind as a side effect.

    Where flock does not work, falls back to the recorded pid — same weaker
    answer `DaemonLock.acquire` settles for, and for the same reason.
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

        O_CREAT without O_TRUNC: truncating before we know we won would wipe
        the running daemon's pid out of the file, so the error we raise could
        not say whose it is. The pid goes in after the lock is ours.
        """
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in _HELD:
                pid = read_pid(self.path)
                os.close(fd)
                raise LockHeld(pid) from exc
            # NFS and some FUSE mounts have no working flock. Refusing to run
            # there would be a worse failure than the race this closes, so
            # carry on unlocked and leave a trace of why the guarantee is gone.
            #
            # Unlocked is not unchecked: the pid in the file still rules out
            # the common case of a daemon that is plainly already running. What
            # is lost is only atomicity — two daemons starting at the same
            # instant can both find the file empty and both win.
            live = _live_daemon_pid(self.path)
            if live is not None:
                os.close(fd)
                raise LockHeld(live) from exc
            logger.warning(
                "cannot lock %s (%s); singleton enforcement is off", self.path, exc
            )
            self.enforced = False
        self._fd = fd
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)

    def release(self) -> None:
        """Drop the lock and remove the pidfile, if it is still ours.

        Unlinking is guarded on the inode still being the one we locked. A
        daemon that dies slowly must not delete the pidfile of the one that
        replaced it — that was half of the bug this module exists to fix.

        Safe to call twice, and on a lock that was never acquired: both leave
        nothing to do.
        """
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        if file_id(self.path) == _fd_id(fd):
            with contextlib.suppress(OSError):
                self.path.unlink()
        # Closing releases the flock. Last, so the file is gone before anyone
        # waiting on the lock can see it free.
        with contextlib.suppress(OSError):
            os.close(fd)


def _fd_id(fd: int) -> tuple[int, int] | None:
    try:
        st = os.fstat(fd)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)
