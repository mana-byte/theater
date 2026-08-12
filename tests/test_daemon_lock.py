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

import os

import pytest

from theater import paths
from theater.client import DaemonClient
from theater.daemon import lock as lock_mod
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


# ---- the daemon ---------------------------------------------------------


async def test_a_second_daemon_refuses_to_start(theater_home, fake_tmux):
    first = Daemon(harnesses={})
    await first.start()
    try:
        second = Daemon(harnesses={})
        with pytest.raises(LockHeld):
            await second.start()
        await second.aclose()
    finally:
        await first.aclose()


async def test_a_refused_daemon_leaves_the_running_one_working(
    theater_home, fake_tmux
):
    """The regression that cost the user two orphans.

    `theater daemon` typed while one is running used to raise, then run its
    shutdown path, which deleted the live daemon's socket and pidfile. The
    live daemon kept running with nothing on disk pointing at it, and the next
    client autostarted a third.
    """
    first = Daemon(harnesses={})
    await first.start()
    try:
        second = Daemon(harnesses={})
        with pytest.raises(LockHeld):
            await second.start()
        await second.aclose()

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


async def test_shutdown_clears_both_files(theater_home, fake_tmux):
    daemon = Daemon(harnesses={})
    await daemon.start()
    assert paths.socket_path().exists()
    assert paths.pidfile_path().exists()
    await daemon.aclose()
    assert not paths.socket_path().exists()
    assert not paths.pidfile_path().exists()
    assert lock_mod.is_free()


async def test_restart_waits_for_the_lock_not_just_the_socket(
    theater_home, fake_tmux
):
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
