"""The daemon: one per machine, owner of the registry.

Singleton by construction. An flock'd pidfile under THEATER_HOME decides who
the daemon is, so a second ``theater daemon`` exits rather than racing the first
for the same SQLite file. See theater/daemon/lock.py for why the lock, and not
the presence of the socket, is the thing consulted.

Concurrency model: one asyncio task per connection, all sharing a single Store
on the loop thread. Store calls are synchronous because they are local and
sub-millisecond; there is no thread pool and no lock, because there is only ever
one thread touching the database.

RPC handlers live in theater/daemon/rpc/ — they are registered via the
@method decorator into the METHODS dict, which this module dispatches from.
theater/daemon/methods.py is a compatibility façade that re-exports the same.

Server runtime concerns are split into theater/daemon/runtime/:
  - ``socket``: path validation, stale-socket clearing, connection dispatch.
  - ``maintenance``: reaper and GC loops.
  - ``lifecycle``: startup, shutdown, reconciliation, and send-seq init.

The Daemon class here composes those modules. Constants and module-level
references used by tests via ``monkeypatch.setattr(server_mod, ...)`` are
re-exported below for compatibility.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from theater import harness as harness_registry
from theater import paths, protocol, timing  # noqa: F401 — compatibility imports
from theater.config import Config
from theater.config import load as load_config
from theater.constants.daemon import CHANNEL_OTEL_RECEIVER_PORT_META_KEY

# Importing methods registers all @method handlers as a side effect.
from theater.daemon import (  # noqa: F401
    methods,
    workers,
)
from theater.daemon.jobs import JobManager
from theater.daemon.lock import DaemonLock
from theater.daemon.observer import Observer
from theater.daemon.registry import Registry
from theater.daemon.rpc import METHODS
from theater.daemon.runtime import lifecycle, maintenance
from theater.daemon.runtime import socket as socket_mod
from theater.daemon.runtime.lifecycle import CLOSE_TIMEOUT, SHUTDOWN_TIMEOUT
from theater.daemon.runtime.maintenance import REAP_INTERVAL
from theater.daemon.runtime.socket import MAX_SOCKET_PATH
from theater.daemon.runtime.tmux_reconcile import reconcile_tmux_inventory
from theater.daemon.spawning.service import Spawner
from theater.daemon.store import Store
from theater.daemon.trajectory import TrajectoryService
from theater.daemon.trajectory.telemetry import AGENT_METRIC_SPECS, create_agent_telemetry
from theater.harness import Harness
from theater.harness.channels.hooks import HookRuntime
from theater.harness.channels.otel import NativeOtelRuntime
from theater.harness.contracts.channels import ChannelKind
from theater.observability import metric_bridge
from theater.tmux import client as tmux  # noqa: F401 — monkeypatched via server_mod

if TYPE_CHECKING:
    from theater.observability import SignalBridge

logger = logging.getLogger("theater.daemon")


@dataclass(frozen=True, slots=True)
class DaemonRunOptions:
    """Typed options for production daemon startup."""

    log_level: str = "INFO"
    timing: bool = False
    stderr_token: str | None = None


def _check_socket_path(sock) -> None:
    socket_mod.check_socket_path(sock, maximum=MAX_SOCKET_PATH)


class Daemon:
    """The singleton daemon process: owns the registry, socket, and maintenance loops.

    Composition root that wires Store, Registry, Spawner, JobManager, and
    Observer, then delegates start/serve/stop/aclose to lifecycle, reaping
    and GC to maintenance, and connection handling to socket transport.
    """

    def __init__(
        self,
        *,
        store: Store | None = None,
        harnesses: dict[str, Harness] | None = None,
        config: Config | None = None,
        lock: DaemonLock | None = None,
        signal_bridge: SignalBridge | None = None,
    ):
        if lock is not None and not lock.held:
            raise ValueError("injected lock must already be held")
        self._lock = lock if lock is not None else DaemonLock()
        _owned_store: Store | None = None
        try:
            paths.ensure_home()
            if lock is None:
                self._lock.acquire()
            self.config = config if config is not None else load_config()
            installed = harness_registry.install(self.config)
            logger.info("harnesses: %s", ", ".join(installed) or "none")
            if store is not None:
                self.store = store
            else:
                _owned_store = Store(paths.db_path())
                self.store = _owned_store
            self.registry = Registry(self.store)
            self.hook_runtime = HookRuntime(self._hook_credential_active)
            self.registry.add_participant_cleanup(self.hook_runtime.drop_participant)
            self.otel_runtime = NativeOtelRuntime(
                self._otel_credential,
                receiver_port_lookup=self._otel_receiver_port,
                receiver_port_store=self._set_otel_receiver_port,
            )
            self.registry.add_participant_cleanup(self.otel_runtime.drop_participant)
            self._tmux_reconcile_lock = asyncio.Lock()
            self.spawner = Spawner(
                self.registry,
                otel_runtime=self.otel_runtime,
                reconcile_tmux=lambda: reconcile_tmux_inventory(self, context="spawn"),
                tmux_reconcile_lock=self._tmux_reconcile_lock,
            )
            self.jobs = JobManager(self.store)
            agent_telemetry = create_agent_telemetry(
                self.store,
                metric_bridge(),
                signal_bridge,
                metrics_enabled=self.config.observability.agent_metrics,
                logs_enabled=self.config.observability.agent_logs,
                spans_enabled=self.config.observability.agent_spans,
                include_log_content=self.config.observability.agent_log_content,
            )
            # ``harnesses={}`` disables observation entirely.
            observer_cfg = self.config.observer
            self.observer = Observer(
                self.registry,
                harnesses,
                poll=observer_cfg.poll_interval,
                search=observer_cfg.search_interval,
                sync=observer_cfg.sync_interval,
                relocate=observer_cfg.relocate_timeout,
                awaiting=observer_cfg.awaiting_input_timeout,
                screen=observer_cfg.screen_interval,
                rescue=observer_cfg.rescue_timeout,
                jobs=self.jobs,
                agent_telemetry=agent_telemetry,
                hook_runtime=self.hook_runtime,
                otel_runtime=self.otel_runtime,
            )
            self.trajectory = TrajectoryService(self.store, self.registry, self.observer)
            self.trajectory_service = self.trajectory
            self._server: asyncio.Server | None = None
            self._reaper: asyncio.Task | None = None
            self._gc: asyncio.Task | None = None
            self._lag: asyncio.Task | None = None
            self._gauge_sampler = None
            self._explicit_kills: set[str] = set()
            self._sock_id: tuple[int, int] | None = None
            self._stopping = asyncio.Event()
            self._conns: set[asyncio.Task] = set()
            self._send_seq = 0
        except BaseException:
            if _owned_store is not None:
                with contextlib.suppress(Exception):
                    _owned_store.close()
            self._lock.release()
            raise

    def _hook_credential_active(self, participant_id: str, channel_id: str) -> bool:
        return (
            self.store.get_channel_credential(participant_id, ChannelKind.HOOK, channel_id)
            is not None
        )

    def _otel_credential(self, participant_id: str, channel_id: str):
        return self.store.get_channel_credential(participant_id, ChannelKind.OTEL, channel_id)

    def _otel_receiver_port(self) -> str | None:
        return self.store.get_meta(CHANNEL_OTEL_RECEIVER_PORT_META_KEY)

    def _set_otel_receiver_port(self, port: int) -> None:
        self.store.set_meta(CHANNEL_OTEL_RECEIVER_PORT_META_KEY, str(port))

    def _next_send_seq(self) -> int:
        return lifecycle.next_send_seq(self)

    def _init_send_seq(self) -> None:
        lifecycle.init_send_seq(self)

    # ---- lifecycle -----------------------------------------------------

    async def start(self) -> None:
        await lifecycle.start(self, check_path=_check_socket_path)

    async def _reconcile(self) -> None:
        await lifecycle.reconcile(self)

    async def serve(self) -> None:
        await lifecycle.serve(self)

    def stop(self) -> None:
        lifecycle.stop(self)

    async def aclose(self) -> None:
        await lifecycle.aclose(
            self,
            close_timeout=CLOSE_TIMEOUT,
            shutdown_workers=workers.shutdown,
        )

    def _release_files(self) -> None:
        lifecycle.release_files(self)

    @staticmethod
    def _clear_stale_socket(sock) -> None:
        socket_mod.clear_stale_socket(sock)

    # ---- reaper --------------------------------------------------------

    async def _reap_loop(self) -> None:
        await maintenance.reap_loop(self, interval=REAP_INTERVAL)

    def _socket_lost(self) -> bool:
        return maintenance.socket_lost(self)

    async def _reap_once(self) -> None:
        await maintenance.reap_once(self)

    # ---- garbage collection --------------------------------------------

    async def _gc_loop(self) -> None:
        await maintenance.gc_loop(self)

    # ---- connection handling -------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await socket_mod.handle_connection(self, reader, writer)

    async def _dispatch(self, line: bytes) -> bytes:
        return await socket_mod.dispatch(self, line, methods=METHODS)


# ---- entrypoint --------------------------------------------------------


async def run(options: DaemonRunOptions | None = None) -> None:
    if options is None:
        options = DaemonRunOptions()
    runtime_handle = None
    daemon = None
    lock = None
    try:
        paths.ensure_home()
        lock = DaemonLock()
        lock.acquire()
        # Prune old raw stderr generations on every winning start.
        from theater.constants.observability import PROCESS_ROLE_DAEMON, STDERR_GENERATIONS
        from theater.observability.logging import generation_path, prune_stderr_generations

        current: Path | None = (
            generation_path(paths.daemon_stderr_logs_dir(), options.stderr_token)
            if options.stderr_token is not None
            else None
        )
        prune_stderr_generations(paths.daemon_stderr_logs_dir(), current, retain=STDERR_GENERATIONS)
        settings = load_config()
        from theater.observability.runtime import configure

        obs = settings.observability
        runtime_handle = configure(
            role=PROCESS_ROLE_DAEMON,
            otlp_enabled=obs.otlp_enabled,
            otlp_protocol=obs.otlp_protocol,
            otlp_endpoint=obs.otlp_endpoint,
            service_name=obs.service_name,
            export_interval_ms=obs.export_interval_ms,
            log_level=options.log_level,
            log_max_bytes=obs.log_max_bytes,
            log_backup_count=obs.log_backup_count,
            log_path=paths.log_path(),
            foreground=options.stderr_token is None,
            metric_specs=AGENT_METRIC_SPECS if obs.agent_metrics else (),
        )
        if options.timing:
            timing.enable_trace()
        lock_to_transfer = lock
        lock = None
        daemon = Daemon(
            config=settings,
            lock=lock_to_transfer,
            signal_bridge=runtime_handle.signal_bridge,
        )
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, daemon.stop)
        await daemon.serve()
    finally:
        try:
            if daemon is not None:
                try:
                    await asyncio.wait_for(daemon.aclose(), SHUTDOWN_TIMEOUT)
                except TimeoutError:
                    logger.error(  # noqa: TRY400
                        "shutdown did not finish within %.0fs; releasing socket and lock",
                        SHUTDOWN_TIMEOUT,
                    )
                    daemon._release_files()
                except BaseException:
                    daemon._release_files()
                    raise
            elif lock is not None:
                lock.release()
        finally:
            if runtime_handle is not None:
                runtime_handle.shutdown()
