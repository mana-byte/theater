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

from theater import harness as harness_registry
from theater import paths
from theater.config import Config
from theater.config import load as load_config

# Importing methods registers all @method handlers as a side effect.
from theater.daemon import (  # noqa: F401
    methods,
)
from theater.daemon.jobs import JobManager
from theater.daemon.lock import DaemonLock
from theater.daemon.observer import Observer
from theater.daemon.registry import Registry
from theater.daemon.rpc import METHODS  # noqa: F401 — re-exported for cold-import surface tests
from theater.daemon.runtime import lifecycle, maintenance
from theater.daemon.runtime import socket as socket_mod
from theater.daemon.runtime.lifecycle import (
    CLOSE_TIMEOUT,  # noqa: F401 — monkeypatched via server_mod
    SHUTDOWN_TIMEOUT,
)
from theater.daemon.runtime.maintenance import (
    REAP_INTERVAL,  # noqa: F401 — monkeypatched via server_mod
)
from theater.daemon.runtime.socket import (
    MAX_SOCKET_PATH,  # noqa: F401 — monkeypatched via server_mod
    clear_stale_socket,
)
from theater.daemon.spawning.service import Spawner
from theater.daemon.store import Store
from theater.harness import Harness
from theater.tmux import client as tmux  # noqa: F401 — monkeypatched via server_mod

logger = logging.getLogger("theater.daemon")


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
    ):
        paths.ensure_home()
        # Held from construction to aclose(). Taken in __init__, not start(),
        # because constructing a Daemon opens the shared SQLite file and runs
        # migrations. The lock must come before the first touch of shared state.
        self._lock = DaemonLock()
        self._lock.acquire()
        try:
            # Read once, never reloaded.
            self.config = config if config is not None else load_config()
            installed = harness_registry.install(self.config)
            logger.info("harnesses: %s", ", ".join(installed) or "none")
            self.store = store or Store(paths.db_path())
            self.registry = Registry(self.store)
            self.spawner = Spawner(self.registry)
            self.jobs = JobManager(self.store)
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
            )
            self._server: asyncio.Server | None = None
            self._reaper: asyncio.Task | None = None
            self._gc: asyncio.Task | None = None
            self._lag: asyncio.Task | None = None
            # Participant ids whose kill is in flight. While a pid is in here,
            # the reaper skips it — the explicit-kill path must win the race.
            self._explicit_kills: set[str] = set()
            # (device, inode) of the bound socket, so shutdown can tell ours
            # from a successor's at the same path.
            self._sock_id: tuple[int, int] | None = None
            self._stopping = asyncio.Event()
            # One task per open connection, so shutdown can end them.
            self._conns: set[asyncio.Task] = set()
            # Monotonic counter for send-job handle uniqueness.
            self._send_seq = 0
        except BaseException:
            # Construction failing leaves no object to close, so nothing would
            # drop the fd. Releasing here lets the next attempt get the lock.
            self._lock.release()
            raise

    def _next_send_seq(self) -> int:
        return lifecycle.next_send_seq(self)

    def _init_send_seq(self) -> None:
        lifecycle.init_send_seq(self)

    # ---- lifecycle -----------------------------------------------------

    async def start(self) -> None:
        await lifecycle.start(self)

    async def _reconcile(self) -> None:
        await lifecycle.reconcile(self)

    async def serve(self) -> None:
        await lifecycle.serve(self)

    def stop(self) -> None:
        lifecycle.stop(self)

    async def aclose(self) -> None:
        await lifecycle.aclose(self)

    def _release_files(self) -> None:
        lifecycle.release_files(self)

    @staticmethod
    def _clear_stale_socket(sock) -> None:
        clear_stale_socket(sock)

    # ---- reaper --------------------------------------------------------

    async def _reap_loop(self) -> None:
        await maintenance.reap_loop(self)

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
        return await socket_mod.dispatch(self, line)


# ---- entrypoint --------------------------------------------------------


async def run() -> None:
    daemon = Daemon()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, daemon.stop)
    try:
        await daemon.serve()
    finally:
        # Bounded, and the socket and lock go regardless.
        try:
            await asyncio.wait_for(daemon.aclose(), SHUTDOWN_TIMEOUT)
        except TimeoutError:
            logger.error(  # noqa: TRY400
                "shutdown did not finish within %.0fs; releasing socket and lock",
                SHUTDOWN_TIMEOUT,
            )
            daemon._release_files()
