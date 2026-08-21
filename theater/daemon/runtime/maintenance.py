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
from theater.daemon.jobs import JobState
from theater.daemon.lock import file_id
from theater.tmux import client as tmux

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
    """Mark tracked participants dead when their pane is gone."""
    tracked = [p for p in daemon.registry.list() if p.tmux_pane]
    if not tracked:
        return
    if not tmux.available():
        return
    try:
        out = await tmux.run("list-panes", "-a", "-F", "#{pane_id}", check=True)
    except Exception as exc:
        logger.warning("reaper: could not list panes: %s", exc)
        return
    alive = set(out.split())
    if not alive:
        logger.warning(
            "reaper: empty pane inventory with %d tracked panes; skipping",
            len(tracked),
        )
        return
    for p in tracked:
        if p.tmux_pane not in alive:
            if p.id in daemon._explicit_kills:
                continue
            logger.info("participant %s lost its pane %s", p.id, p.tmux_pane)
            try:
                await daemon.spawner.retire(p, delete_branch=False)
            except Exception:
                logger.exception("retire failed for %s; marking dead anyway", p.id)
            daemon.registry.mark_dead(p.id)
            running = daemon.store.running_jobs_for_target(p.id)
            for job in running:
                daemon.jobs.finish(job.handle, state=JobState.CRASHED, error_code="crashed")


async def reap_loop(daemon) -> None:
    """Poll for vanished panes until the daemon stops.

    Polling, not tmux hooks. A hook would make correctness depend on state
    inside the user's tmux config, which survives neither kill-server nor
    a config reload.
    """
    while not daemon._stopping.is_set():
        if socket_lost(daemon):
            logger.warning("our socket is gone; nothing can reach us, stopping")
            daemon.stop()
            return
        try:
            await reap_once(daemon)
        except Exception:
            logger.exception("reaper iteration failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(daemon._stopping.wait(), timeout=REAP_INTERVAL)


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
