"""Daemon lifecycle: startup, reconciliation, and shutdown orchestration.

The Daemon class composes these functions as its start/serve/stop/aclose
path. Separated from server.py so the ordering invariants — lock before
socket, observer last to start, socket first to release — live in a
module that owns nothing else.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging

from theater import paths, protocol, timing
from theater.constants.observability import (
    JOBS_ACTIVE_GAUGE,
    PARTICIPANTS_ADDRESSABLE_GAUGE,
    PARTICIPANTS_LIVE_GAUGE,
)
from theater.daemon.jobs import JobState
from theater.daemon.lock import file_id
from theater.models import Status
from theater.observability.metrics import create_active_gauge_sampler
from theater.tmux import client as tmux

logger = logging.getLogger("theater.daemon")

#: How long aclose() waits for the listener to finish closing.
CLOSE_TIMEOUT = 2.0

#: How long run() gives the whole shutdown before it gives up.
SHUTDOWN_TIMEOUT = 45.0


def init_send_seq(daemon) -> None:
    """Initialize the send sequence from the database.

    After a restart, the counter must not reuse handle numbers that already
    exist in the jobs table. The persisted meta value protects against a
    future GC that deletes the highest-numbered job rows.
    """
    try:
        persisted = daemon.store.get_send_seq()
        highest = max(persisted, daemon.store.max_send_seq())
        if highest:
            daemon._send_seq = highest
            logger.info("send sequence initialized to %d", daemon._send_seq)
    except Exception as exc:
        logger.debug("could not initialize send sequence: %s", exc)


def next_send_seq(daemon) -> int:
    daemon._send_seq += 1
    try:
        daemon.store.set_send_seq(daemon._send_seq)
    except Exception:
        logger.debug("could not persist send sequence", exc_info=True)
    return daemon._send_seq


async def start(daemon, *, check_path) -> None:
    """Bind the socket. Raises here, in the caller's face, if it cannot."""
    sock = paths.socket_path()
    check_path(sock)
    try:
        daemon._clear_stale_socket(sock)
        daemon._server = await asyncio.start_unix_server(
            daemon._handle, path=str(sock), limit=protocol.MAX_MESSAGE_BYTES
        )
        sock.chmod(0o600)
    except BaseException:
        daemon._lock.release()
        raise
    daemon._sock_id = file_id(sock)
    await daemon._reconcile()
    daemon._init_send_seq()
    await _start_gauge_sampler(daemon)
    daemon._reaper = asyncio.create_task(daemon._reap_loop())
    daemon._lag = asyncio.create_task(timing.lag_monitor(daemon._stopping))
    if daemon.config.retention.enabled:
        daemon._gc = asyncio.create_task(daemon._gc_loop())
    daemon.observer.start()
    logger.info("listening on %s", sock)


async def reconcile(daemon) -> None:
    """Rebuild in-memory state and reconcile with tmux after a restart.

    SQLite already holds the participants, jobs, and bus. What is lost on
    restart is the in-memory asyncio Events for jobs and the observer tasks.
    """
    if not tmux.available():
        logger.info("tmux unavailable; skipping reconciliation")
        return
    tracked = [p for p in daemon.registry.list() if p.tmux_pane]
    try:
        out = await tmux.run("list-panes", "-a", "-F", "#{pane_id}", check=True)
        alive_panes = set(out.split())
    except Exception as exc:
        logger.warning("reconcile: could not list panes: %s", exc)
        return
    if not alive_panes and tracked:
        logger.warning(
            "reconcile: empty pane inventory with %d tracked panes; skipping",
            len(tracked),
        )
        return

    for p in daemon.registry.list():
        if p.tmux_pane and p.tmux_pane not in alive_panes and p.status is not Status.DEAD:
            logger.info("reconcile: %s lost its pane %s", p.id, p.tmux_pane)
            await daemon.spawner.retire(p, delete_branch=False)
            daemon.registry.mark_dead(p.id)

    for p in daemon.registry.list(include_dead=True):
        if p.status is Status.DEAD:
            running = daemon.store.running_jobs_for_target(p.id)
            for job in running:
                daemon.jobs.finish(job.handle, state=JobState.CRASHED, error_code="crashed")

    for p in daemon.registry.list():
        if p.status is not Status.DEAD:
            running = daemon.store.running_jobs_for_target(p.id)
            for job in running:
                if job.handle not in daemon.jobs._events:
                    daemon.jobs._events[job.handle] = asyncio.Event()

    logger.info(
        "reconcile complete: %d participants, %d live panes",
        len(daemon.registry.list(include_dead=True)),
        len(alive_panes),
    )


async def serve(daemon) -> None:
    """Run until stop() is called. Teardown is aclose()'s job, not ours.

    Deliberately not ``async with self._server``: Server.__aexit__ calls
    wait_closed(), which since 3.12 waits for every connection handler to
    finish — and our handlers only finish when their client disconnects.
    """
    await daemon.start()
    assert daemon._server is not None
    await daemon._stopping.wait()


def stop(daemon) -> None:
    daemon._stopping.set()


async def _start_gauge_sampler(daemon) -> None:
    sources = {
        PARTICIPANTS_LIVE_GAUGE: daemon.registry.live_count,
        PARTICIPANTS_ADDRESSABLE_GAUGE: daemon.registry.addressable_count,
        JOBS_ACTIVE_GAUGE: daemon.jobs.active_count,
    }
    interval = daemon.config.observability.gauge_interval_s
    sampler = create_active_gauge_sampler(interval, sources)
    if sampler is None:
        return
    await sampler.start()
    daemon._gauge_sampler = sampler


async def aclose(daemon, *, close_timeout: float, shutdown_workers) -> None:
    """Shut down in the one order that terminates."""
    daemon.stop()
    if daemon._server:
        daemon._server.close()
    trajectory = getattr(daemon, "trajectory", None)
    if trajectory is not None:
        close = getattr(trajectory, "aclose", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result
    await daemon.observer.aclose()
    await daemon.hook_runtime.aclose()
    if daemon._reaper:
        daemon._reaper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await daemon._reaper
    if daemon._gc:
        daemon._gc.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await daemon._gc
    if daemon._lag:
        daemon._lag.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await daemon._lag
    for task in list(daemon._conns):
        task.cancel()
    if daemon._conns:
        await asyncio.gather(*daemon._conns, return_exceptions=True)
        daemon._conns.clear()
    await shutdown_workers()
    sampler = getattr(daemon, "_gauge_sampler", None)
    if sampler is not None:
        await sampler.stop()
        daemon._gauge_sampler = None
    if daemon._server:
        try:
            await asyncio.wait_for(daemon._server.wait_closed(), close_timeout)
        except TimeoutError:
            logger.warning(
                "listener did not close within %.1fs; releasing anyway",
                close_timeout,
            )
    daemon.store.close()
    daemon._release_files()


def release_files(daemon) -> None:
    """Delete the socket and pidfile — but only if they are still ours.

    A daemon can take seconds to shut down: the observer stops, connections
    drain. A replacement can be listening before that finishes. Both
    deletions are guarded on identity, and the socket goes first.
    """
    sock = paths.socket_path()
    if daemon._sock_id is not None and file_id(sock) == daemon._sock_id:
        with contextlib.suppress(OSError):
            sock.unlink()
    daemon._sock_id = None
    daemon._lock.release()
