"""Only one daemon, and a dying one takes nothing with it.

Two bugs lived here, and both produced the same symptom for the user: after a
`theater restart`, no socket on disk and two orphan daemons still alive.

  1. Startup unlinked the socket by path after a failed probe. Two daemons
     starting together could both pass the probe; the second deleted the
     first's fresh socket and bound its own, leaving the first listening on an
     unreachable inode.
  2. Shutdown unlinked the socket and pidfile by path, unconditionally. A
     daemon that took seconds to die therefore deleted its replacement's files.
     Worse, a daemon that refused to start still ran that shutdown path, so
     `theater daemon` typed twice removed the running daemon's socket.

The tests below are written against those two behaviours rather than against
the implementation, so they would survive swapping flock for something else.
"""

from __future__ import annotations

import errno
import os

import pytest

from theater import paths
from theater.client import DaemonClient
from theater.daemon import lock as lock_mod
from theater.daemon import server as server_mod
from theater.daemon.lock import DaemonLock, LockHeld
from theater.daemon.server import Daemon

# ---- the lock itself ---------------------------------------------------


def test_a_second_lock_on_the_same_file_is_refused(theater_home):
    first = DaemonLock()
    first.acquire()
    try:
        with pytest.raises(LockHeld):
            DaemonLock().acquire()
    finally:
        first.release()


def test_the_refusal_names_the_holder(theater_home):
    first = DaemonLock()
    first.acquire()
    try:
        with pytest.raises(LockHeld) as caught:
            DaemonLock().acquire()
    finally:
        first.release()
    assert caught.value.pid == os.getpid()
    assert str(os.getpid()) in str(caught.value)


def test_releasing_frees_it_for_the_next_daemon(theater_home):
    first = DaemonLock()
    first.acquire()
    first.release()
    second = DaemonLock()
    second.acquire()  # must not raise
    second.release()


def test_release_removes_the_pidfile(theater_home):
    lock = DaemonLock()
    lock.acquire()
    assert paths.pidfile_path().exists()
    lock.release()
    assert not paths.pidfile_path().exists()


def test_release_leaves_a_successors_pidfile_alone(theater_home):
    """The half of the bug that deleted live state.

    A late-dying daemon calls release() after its replacement has already
    written its own pidfile. Same path, different inode: the deletion must not
    happen.
    """
    dying = DaemonLock()
    dying.acquire()
    paths.pidfile_path().unlink()  # the successor replaced the file
    successor = DaemonLock()
    successor.acquire()
    dying.release()
    assert paths.pidfile_path().exists()
    assert lock_mod.read_pid() == os.getpid()
    successor.release()


def test_release_is_idempotent(theater_home):
    lock = DaemonLock()
    lock.acquire()
    lock.release()
    lock.release()  # must not raise


def test_release_without_acquire_does_nothing(theater_home):
    DaemonLock().release()
    assert not paths.pidfile_path().exists()


def test_is_free_tracks_the_holder(theater_home):
    assert lock_mod.is_free()
    lock = DaemonLock()
    lock.acquire()
    assert not lock_mod.is_free()
    lock.release()
    assert lock_mod.is_free()


def test_is_free_does_not_create_the_pidfile(theater_home):
    """It is a question. Asking it must not leave state behind."""
    assert lock_mod.is_free()
    assert not paths.pidfile_path().exists()


# ---- filesystems without flock ------------------------------------------
#
# NFS and some FUSE mounts return ENOLCK rather than honouring the lock. The
# daemon runs there anyway — refusing would be a worse failure than the race —
# but "cannot lock" must not degrade all the way to "cannot notice a daemon
# that is plainly already running". These cover the weaker fallback.


class _Probe:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout


def _unlockable(monkeypatch):
    def refuse(fd, op):
        raise OSError(errno.ENOLCK, "no locks available")

    monkeypatch.setattr(lock_mod.fcntl, "flock", refuse)


def test_live_pid_ignores_a_number_that_belongs_to_something_else(theater_home, monkeypatch):
    """Pid reuse is the reason this is not `os.kill(pid, 0)`.

    After a hard kill the number is free for the OS to hand to anything; a
    shell that inherited it would answer a signal probe and read as a daemon
    for as long as it lived.
    """
    paths.pidfile_path().write_text("4242\n")
    monkeypatch.setattr(lock_mod.subprocess, "run", lambda *a, **k: _Probe(0, "-zsh\n"))
    assert lock_mod._live_daemon_pid(paths.pidfile_path()) is None

    monkeypatch.setattr(
        lock_mod.subprocess,
        "run",
        lambda *a, **k: _Probe(0, "python -m theater.cli daemon\n"),
    )
    assert lock_mod._live_daemon_pid(paths.pidfile_path()) == 4242


def test_live_pid_is_none_when_the_process_is_gone(theater_home, monkeypatch):
    paths.pidfile_path().write_text("4242\n")
    monkeypatch.setattr(lock_mod.subprocess, "run", lambda *a, **k: _Probe(1, ""))
    assert lock_mod._live_daemon_pid(paths.pidfile_path()) is None


def test_live_pid_degrades_to_none_when_ps_cannot_run(theater_home, monkeypatch):
    """Neither locking nor `ps`: answer "free" so the machine can still work."""
    paths.pidfile_path().write_text("4242\n")

    def explode(*a, **k):
        raise OSError("no ps here")

    monkeypatch.setattr(lock_mod.subprocess, "run", explode)
    assert lock_mod._live_daemon_pid(paths.pidfile_path()) is None


def test_without_flock_a_live_pid_still_refuses(theater_home, monkeypatch):
    paths.pidfile_path().write_text("4242\n")
    _unlockable(monkeypatch)
    monkeypatch.setattr(lock_mod, "_live_daemon_pid", lambda path: 4242)

    with pytest.raises(LockHeld) as caught:
        DaemonLock().acquire()
    assert caught.value.pid == 4242
    assert not lock_mod.is_free()


def test_without_flock_a_dead_pid_lets_the_daemon_run(theater_home, monkeypatch):
    """Degraded, not broken: unenforced, but running."""
    paths.pidfile_path().write_text("4242\n")
    _unlockable(monkeypatch)
    monkeypatch.setattr(lock_mod, "_live_daemon_pid", lambda path: None)

    lock = DaemonLock()
    lock.acquire()
    try:
        assert lock.held
        assert not lock.enforced
        assert lock_mod.is_free()
    finally:
        lock.release()


# ---- the daemon ---------------------------------------------------------


async def test_a_refused_daemon_never_opens_the_database(theater_home, fake_tmux, monkeypatch):
    """Losing the race must happen before any shared state is touched.

    Constructing a Daemon runs Alembic migrations against the shared SQLite
    file. While the lock was taken in start(), both daemons migrated and only
    then found out which of them was allowed to exist — two writers on one
    database, with the loser's schema work already committed.
    """
    opened: list[str] = []
    real_store = server_mod.Store

    def counting_store(path, *args, **kwargs):
        opened.append(str(path))
        return real_store(path, *args, **kwargs)

    monkeypatch.setattr(server_mod, "Store", counting_store)

    first = Daemon(harnesses={})
    await first.start()
    try:
        assert len(opened) == 1
        with pytest.raises(LockHeld):
            Daemon(harnesses={})
        assert len(opened) == 1
    finally:
        await first.aclose()


async def test_a_failed_construction_releases_the_lock(theater_home, monkeypatch):
    """A Daemon that raises mid-__init__ leaves the lock free.

    The caller gets no object back, so nothing else can release it. Without
    the guard the fd would survive until garbage collection and every later
    attempt in this process would refuse itself.
    """

    def boom(*args, **kwargs):
        raise RuntimeError("no database today")

    monkeypatch.setattr(server_mod, "Store", boom)

    with pytest.raises(RuntimeError, match="no database today"):
        Daemon(harnesses={})
    assert lock_mod.is_free()


async def test_a_refused_daemon_leaves_the_running_one_working(theater_home, fake_tmux):
    """The regression that cost the user two orphans.

    `theater daemon` typed while one is running used to raise, then run its
    shutdown path, which deleted the live daemon's socket and pidfile. The
    live daemon kept running with nothing on disk pointing at it, and the next
    client autostarted a third.
    """
    first = Daemon(harnesses={})
    await first.start()
    try:
        with pytest.raises(LockHeld):
            Daemon(harnesses={})

        assert paths.socket_path().exists()
        assert paths.pidfile_path().exists()
        async with DaemonClient(autostart=False) as c:
            assert await c.call("ping")
    finally:
        await first.aclose()


async def test_shutdown_leaves_a_successors_socket_alone(theater_home, fake_tmux):
    """A slow shutdown must not disconnect the daemon that replaced it."""
    dying = Daemon(harnesses={})
    await dying.start()
    # The successor: same path, different socket. Reached by taking the files
    # away from the first daemon, which is exactly the state a hard kill and
    # restart produces.
    paths.socket_path().unlink()
    paths.pidfile_path().unlink()
    successor = Daemon(harnesses={})
    await successor.start()
    try:
        await dying.aclose()
        assert paths.socket_path().exists()
        assert paths.pidfile_path().exists()
        async with DaemonClient(autostart=False) as c:
            assert await c.call("ping")
    finally:
        await successor.aclose()


async def test_a_daemon_starts_over_a_stale_socket(theater_home, fake_tmux):
    """kill -9 leaves the socket file behind. The next daemon must clear it."""
    paths.socket_path().write_bytes(b"")  # not a socket, and nothing listening
    daemon = Daemon(harnesses={})
    await daemon.start()
    try:
        async with DaemonClient(autostart=False) as c:
            assert await c.call("ping")
    finally:
        await daemon.aclose()


async def test_restart_waits_for_the_lock_not_just_the_socket(theater_home, fake_tmux):
    """`theater restart` used to watch the socket, which a hard kill never removes.

    Holding the lock with the socket already gone is the shape of a daemon
    partway through shutdown. A replacement started then would lose the race
    for the lock, so the wait has to see it as still running.
    """
    from theater import cli

    lock = DaemonLock()
    lock.acquire()
    try:
        assert not paths.socket_path().exists()
        assert not cli._daemon_released()
        assert not cli._await_daemon_gone(timeout=0.1)
    finally:
        lock.release()
    assert cli._daemon_released()
