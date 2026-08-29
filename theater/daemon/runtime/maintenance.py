"""Maintenance loops: the reaper and garbage collection.

Both loops poll on a timer and log-and-continue on error. The reaper marks
participants dead once their tmux pane is gone; the GC sweeps old database
rows on the retention interval. The daemon delegates to this module so the
loop bodies are cohesive and separately testable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from theater import paths
from theater.daemon.lock import file_id
from theater.daemon.runtime.tmux_reconcile import reconcile_tmux_inventory

logger = logging.getLogger("theater.daemon")

#: How often to check whether panes we know about still exist.
REAP_INTERVAL = 1.0


def socket_lost(daemon) -> bool:
    """True once the path we bound no longer leads to our socket.

    Deleting the socket file does not close the listening socket: the daemon
    keeps running on an inode nobody can open, still holding the lock, so
    every client autostarts a replacement that the lock then refuses.
    Identity, not existence: a successor that bound a new socket at the same
    path is also a reason to go.
    """
    if daemon._sock_id is None:
        return False
    return file_id(paths.socket_path()) != daemon._sock_id


async def reap_once(daemon) -> None:
    """Reconcile tracked panes against one server-identity inventory."""
    await reconcile_tmux_inventory(daemon, context="reaper")


async def reap_loop(daemon, *, interval: float) -> None:
    """Poll for vanished panes until the daemon stops.

    Polling, not tmux hooks. A hook would make correctness depend on state
    inside the user's tmux config, which survives neither kill-server nor
    a config reload.
    """
    while not daemon._stopping.is_set():
        if daemon._socket_lost():
            logger.warning("our socket is gone; nothing can reach us, stopping")
            daemon.stop()
            return
        try:
            await daemon._reap_once()
        except Exception:
            logger.exception("reaper iteration failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(daemon._stopping.wait(), timeout=interval)


async def gc_loop(daemon) -> None:
    """Bound the database size by sweeping old rows on a timer.

    Not in the reaper: that method early-returns when there are no tracked
    panes or tmux is unavailable — precisely the idle machine where GC
    should run. Waits before the first sweep: GC writes, and on a freshly
    started daemon reconcile may still be settling.
    """
    from theater.daemon.gc import sweep

    retention = daemon.config.retention
    while not daemon._stopping.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(daemon._stopping.wait(), timeout=retention.interval)
        if daemon._stopping.is_set():
            return
        try:
            result = await sweep(
                daemon.store,
                retention,
                live_handles=frozenset(daemon.jobs._events),
            )
            if (
                result.bus
                or result.jobs
                or result.touch
                or result.participants
                or result.running_marked
                or result.scratchpad
            ):
                logger.info(
                    "gc sweep: %d bus, %d jobs, %d touch, "
                    "%d participants, %d running marked, %d scratchpad",
                    result.bus,
                    result.jobs,
                    result.touch,
                    result.participants,
                    result.running_marked,
                    result.scratchpad,
                )
        except Exception:
            logger.exception("gc sweep failed")
