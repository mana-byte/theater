"""Native-runtime retry and garbage-collection maintenance loops."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from theater import paths
from theater.daemon.lock import file_id

logger = logging.getLogger("theater.daemon")

#: How often to retry teardown of native backends owned by dead participants.
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
    """Retry exact native backend teardown for already-dead participants."""
    from theater.daemon.runtime import recovery

    # A dead participant owns no live backend: the reaper retries teardowns
    # that failed earlier and sweeps bindings left by failed spawns. Explicit
    # in-flight kills are left alone; the kill flow owns those.
    await recovery.sweep_dead_participant_backends(daemon)


async def reap_loop(daemon, *, interval: float) -> None:
    """Retry native teardown until the daemon stops."""
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

    It waits before the first sweep because GC writes while startup recovery
    may still be settling.
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
