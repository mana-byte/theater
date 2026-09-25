"""Daemon lifecycle: startup, reconciliation, and shutdown orchestration.

Kept apart so the ordering invariants — lock before socket, observer last to start,
socket first to release — live in a module that owns nothing else.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging

from theater import paths, protocol, timing
from theater.constants.observability import (
    CONTROL_QUEUE_DEPTH_GAUGE,
    JOBS_ACTIVE_GAUGE,
    PARTICIPANTS_ADDRESSABLE_GAUGE,
    PARTICIPANTS_LIVE_GAUGE,
)
from theater.daemon.jobs import JobState
from theater.daemon.lock import file_id
from theater.models import Status
from theater.observability.metrics import create_active_gauge_sampler

logger = logging.getLogger("theater.daemon")

#: How long aclose() waits for the listener to finish closing.
CLOSE_TIMEOUT = 2.0

#: How long run() gives the whole shutdown before it gives up.
SHUTDOWN_TIMEOUT = 45.0


def init_send_seq(daemon) -> None:
    """Initialize the send sequence from the database.

    Never reuse handle numbers; the persisted meta value survives a GC of the top job rows.
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
    from theater.daemon.runtime import recovery

    sock = paths.socket_path()
    check_path(sock)
    begin_provider_recovery = getattr(daemon.terminal_service, "begin_startup_recovery", None)
    if callable(begin_provider_recovery):
        begin_provider_recovery()
    try:
        if await daemon.otel_runtime.start(daemon.observer.harnesses):
            daemon.otel_runtime.restore(daemon.registry.list(), daemon.observer.harnesses)
        daemon._clear_stale_socket(sock)
        daemon._server = await asyncio.start_unix_server(
            daemon._handle, path=str(sock), limit=protocol.MAX_MESSAGE_BYTES
        )
        sock.chmod(0o600)
    except BaseException:
        daemon._lock.release()
        raise
    daemon._sock_id = file_id(sock)
    configure_presence = getattr(daemon.presence, "configure_terminal_service", None)
    if callable(configure_presence):
        from theater.daemon.presence.lifecycle import retire_authoritative_exit

        async def on_provider_exit(evidence) -> bool:
            return await retire_authoritative_exit(daemon, evidence)

        configure_presence(daemon.terminal_service, exit_handler=on_provider_exit)
    configure_observer = getattr(daemon.observer, "set_terminal_evidence_provider", None)
    if callable(configure_observer):
        configure_observer(daemon.presence)
    configure_provider_recovery = getattr(daemon.terminal_service, "configure_recovery", None)
    if callable(configure_provider_recovery):
        configure_provider_recovery(controls=daemon.controls, jobs=daemon.jobs)
    # Recovery can inspect durable prompt uncertainty before observation is
    # live, but it must not let an already-expired deadline finish a job until
    # the observer has had a bounded chance to route its buffered exact
    # evidence.  ControlService.start() arms that window after observer.start.
    daemon.controls.begin_recovery()
    recovery.prepare_provider_control_recovery(daemon)
    await daemon._reconcile()
    await recovery.reconcile_public_control_operations(daemon)
    finish_provider_recovery = getattr(daemon.terminal_service, "finish_startup_recovery", None)
    if callable(finish_provider_recovery):
        finish_provider_recovery()
    daemon._init_send_seq()
    await _start_gauge_sampler(daemon)
    daemon._reaper = asyncio.create_task(daemon._reap_loop())
    daemon._lag = asyncio.create_task(timing.lag_monitor(daemon._stopping))
    if daemon.config.retention.enabled:
        daemon._gc = asyncio.create_task(daemon._gc_loop())
    # Presence before controls: no queued work may dispatch until the
    # monitor is live, so its absence gate can never be bypassed.
    await daemon.presence.start()
    daemon.observer.start()
    daemon.controls.start(participant.id for participant in daemon.registry.list())
    logger.info("listening on %s", sock)


async def reconcile(daemon) -> None:
    """Rebuild in-memory state before provider generation reconciliation.

    Runtime bindings reconcile first, so native backends and sessions are decided before
    ordinary observation can assume a backend is missing.
    """
    from theater.daemon.runtime import recovery

    await recovery.reconcile_runtime_bindings(daemon)
    presence = getattr(daemon, "presence", None)
    if presence is not None:
        await presence.reconcile()

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
        "reconcile complete: %d participants",
        len(daemon.registry.list(include_dead=True)),
    )


async def serve(daemon) -> None:
    """Run until stop() is called. Teardown is aclose()'s job, not ours.

    Not ``async with self._server``: since 3.12 its exit waits for every connection handler,
    and ours only finish when their client disconnects.
    """
    await daemon.start()
    assert daemon._server is not None
    await daemon._stopping.wait()


def stop(daemon) -> None:
    daemon._stopping.set()


def _queued_followup_depth(daemon) -> int:
    """Total pending Theater followups across every participant's queue.

    Aggregate because per-participant gauge labels are unbounded and forbidden; read on the
    loop via the GaugeSampler (exporters read the cache, never the store), fail-open.
    """
    total = 0
    for participant in daemon.registry.list(include_dead=True):
        total += daemon.store.queued_control_operation_count(participant.id)
    return total


async def _start_gauge_sampler(daemon) -> None:
    sources = {
        PARTICIPANTS_LIVE_GAUGE: daemon.registry.live_count,
        PARTICIPANTS_ADDRESSABLE_GAUGE: daemon.registry.addressable_count,
        JOBS_ACTIVE_GAUGE: daemon.jobs.active_count,
        CONTROL_QUEUE_DEPTH_GAUGE: lambda: _queued_followup_depth(daemon),
    }
    interval = daemon.config.observability.gauge_interval_s
    sampler = create_active_gauge_sampler(interval, sources)
    if sampler is None:
        return
    await sampler.start()
    daemon._gauge_sampler = sampler


async def _aclose_service(service) -> None:
    """Close one optional service; a missing or sync close is not an error."""
    if service is None:
        return
    close = getattr(service, "aclose", None)
    if callable(close):
        result = close()
        if inspect.isawaitable(result):
            await result


async def aclose(daemon, *, close_timeout: float, shutdown_workers) -> None:
    """Shut down in the one order that terminates."""
    daemon.stop()
    # Stop reconnect attempts before the first shutdown await. Otherwise a
    # disconnected runtime can reconnect while unrelated services drain.
    await daemon.runtime_manager.stop_recovery()
    if daemon._server:
        daemon._server.close()
    # Detached public operations own tasks independently of request handlers.
    # Settle cancellation uncertainty while their dependencies and Store remain live.
    await _aclose_service(getattr(daemon, "operation_service", None))
    await _aclose_service(getattr(daemon, "state_service", None))
    await _aclose_service(getattr(daemon, "terminal_service", None))
    # Control-maintenance tasks can be awaiting runtime I/O.  Cancel and
    # await them before either observation or runtime clients are torn down,
    # so no service-owned task outlives the daemon's Store/event loop.
    await daemon.controls.aclose()
    await _aclose_service(getattr(daemon, "presence", None))
    await _aclose_service(getattr(daemon, "trajectory", None))
    await daemon.observer.aclose()
    await _aclose_service(getattr(daemon, "frontend_runtime_host", None))
    # Runtime clients disconnect only; healthy detached backends and UIs stay
    # alive for the next daemon start to adopt.
    await daemon.runtime_manager.aclose()
    await daemon.otel_runtime.aclose()
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

    A replacement may be listening before our slow shutdown ends; both deletions are
    identity-guarded, socket first.
    """
    sock = paths.socket_path()
    if daemon._sock_id is not None and file_id(sock) == daemon._sock_id:
        with contextlib.suppress(OSError):
            sock.unlink()
    daemon._sock_id = None
    daemon._lock.release()
