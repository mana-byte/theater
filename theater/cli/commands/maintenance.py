"""Maintenance commands: gc, stop, restart."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time

from theater import paths
from theater.cli.render import _format_bytes, _format_floor
from theater.client import DaemonClient, call_sync
from theater.constants.cli import CLI_STOP_TIMEOUT_SECONDS as STOP_TIMEOUT


def cmd_gc(args) -> int:
    """Run a garbage-collection sweep now and report what was removed.

    Deleting rows does not shrink the database file — only ``--vacuum``
    does, by rewriting it under an exclusive lock. Without saying that, a
    user who runs ``theater gc`` and checks ``ls -l theater.db`` will report
    GC as broken.
    """
    data = call_sync("gc", vacuum=args.vacuum)
    assert isinstance(data, dict)
    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    bus = data.get("bus", 0)
    jobs = data.get("jobs", 0)
    touch = data.get("touch", 0)
    participants = data.get("participants", 0)
    running_marked = data.get("running_marked", 0)
    scratchpad = data.get("scratchpad", 0)
    total = bus + jobs + touch + participants + running_marked + scratchpad

    if total == 0:
        print("nothing to collect — database is already within retention")
    else:
        print(
            f"collected: {bus} bus, {jobs} jobs, {touch} touch, "
            f"{participants} participants, {running_marked} stale running marked, "
            f"{scratchpad} scratchpad"
        )

    coverage = data.get("coverage") or {}
    print()
    print(f"coverage: jobs from {_format_floor(coverage.get('jobs_from'))}")
    print(f"          bus from {_format_floor(coverage.get('bus_from'))}")

    before = data.get("db_bytes_before", 0)
    after = data.get("db_bytes_after", 0)
    print(f"\ndatabase: {_format_bytes(before)} -> {_format_bytes(after)}")

    vacuum_ran = data.get("vacuum_ran", False)
    if vacuum_ran:
        reclaimed = before - after
        if reclaimed > 0:
            print(f"vacuum reclaimed {_format_bytes(reclaimed)}")
        else:
            print("vacuum ran — file size unchanged (nothing to reclaim)")
    elif total > 0:
        # Without this line, a user who deleted 94% and saw no shrink reports GC as broken.
        print(
            "file size unchanged — deleting rows does not shrink the file; "
            "use `theater gc --vacuum` to reclaim space"
        )
    return 0


def _shutdown_running_daemon() -> bool:
    """Ask a running daemon to stop. False when there was none to ask.

    Autostart off, which is not a detail: the previous version used the
    autostarting client, so `theater stop` with nothing running would launch a
    daemon purely to tell it to shut down.

    Connecting is what answers "is there a daemon"; the call is not. A daemon
    that shuts down promptly may cancel this very connection before its reply
    is drained, and reporting that as "no daemon running" told the user the
    opposite of what had just happened. So the connect is allowed to raise and
    the call is not.
    """

    async def go():
        async with DaemonClient(autostart=False) as client:
            await client.connect()
            with contextlib.suppress(ConnectionError, OSError):
                await client.call("shutdown")

    try:
        asyncio.run(go())
    except (FileNotFoundError, ConnectionRefusedError, ConnectionError, OSError):
        return False
    return True


def _daemon_released() -> bool:
    """True once the old daemon holds neither the socket nor the lock.

    Both, because they answer different questions. The socket is what the next
    client connects to, so a leftover one means a replacement could reach the
    dying daemon. The lock is what the next daemon needs to take, and it is the
    only reliable signal: a daemon killed with -9 leaves its socket file behind
    forever but loses its lock the moment it dies.
    """
    from theater.daemon import lock

    return not paths.socket_path().exists() and lock.is_free()


def _await_daemon_gone(timeout: float | None = None) -> bool:
    """Wait for the stopping daemon to release what a replacement needs.

    The default is read at call time, not bound as a default argument, so the
    wait is patchable — otherwise a test for the timeout path has to take the
    full timeout.
    """
    deadline = time.monotonic() + (STOP_TIMEOUT if timeout is None else timeout)
    while not _daemon_released() and time.monotonic() < deadline:
        time.sleep(0.05)
    return _daemon_released()


def cmd_stop(args) -> int:
    if not _shutdown_running_daemon():
        print("no daemon running")
        return 0
    print("daemon stopping")
    return 0


def cmd_restart(args) -> int:
    """Stop the daemon and start a fresh one.

    This is how a config edit takes effect — config is read once at start and
    never reloaded. Nothing else is disturbed: agents live in tmux panes this
    process does not touch, and the registry is on disk, so the new daemon
    comes back to the same participants.
    """
    if _shutdown_running_daemon() and not _await_daemon_gone():
        held = paths.socket_path() if paths.socket_path().exists() else paths.pidfile_path()
        print(
            f"theater: daemon still holding {held} after {STOP_TIMEOUT:g}s "
            "— not starting a second one",
            file=sys.stderr,
        )
        return 1
    # Autostart does the starting; the ping makes "started" a fact.
    call_sync("ping")
    print("daemon restarted")
    return 0
