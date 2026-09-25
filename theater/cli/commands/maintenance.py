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

# Definition re-exported by the cli facade; runtime reads the facade for legacy patches.
from theater.constants.cli import CLI_STOP_TIMEOUT_SECONDS as STOP_TIMEOUT  # noqa: F401


def _stop_timeout() -> float:
    from theater import cli as _facade

    return _facade.STOP_TIMEOUT


def cmd_gc(args) -> int:
    """Run a garbage-collection sweep now and report what was removed.
    Deleting rows does not shrink the database file — only ``--vacuum`` does (exclusive lock); say
    so, or users checking ``ls -l`` report GC as broken.
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
    Autostart off, so ``stop`` never launches a daemon. Only the connect may raise: a promptly
    exiting daemon can drop the call, which must not read as "no daemon running".
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
    The socket is what clients reach; the lock is the reliable signal, since kill -9 leaves the
    socket file behind but releases the lock.
    """
    from theater.daemon import lock

    return not paths.socket_path().exists() and lock.is_free()


def _await_daemon_gone(timeout: float | None = None) -> bool:
    """Wait for the stopping daemon to release what a replacement needs.

    The default is read at call time so tests can patch the timeout.
    """
    deadline = time.monotonic() + (_stop_timeout() if timeout is None else timeout)
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
    How config edits take effect (config is read once); providers, runtimes, and the registry
    survive.
    """
    if _shutdown_running_daemon() and not _await_daemon_gone():
        held = paths.socket_path() if paths.socket_path().exists() else paths.pidfile_path()
        print(
            f"theater: daemon still holding {held} after {_stop_timeout():g}s "
            "— not starting a second one",
            file=sys.stderr,
        )
        return 1
    # Autostart does the starting; the ping makes "started" a fact.
    call_sync("ping")
    print("daemon restarted")
    return 0
