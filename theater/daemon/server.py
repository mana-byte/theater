"""The daemon: one per machine, owner of the registry.

Singleton by construction. An flock'd pidfile under THEATER_HOME decides who
the daemon is, so a second `theater daemon` exits rather than racing the first
for the same SQLite file. See theater/daemon/lock.py for why the lock, and not
the presence of the socket, is the thing consulted.

Concurrency model: one asyncio task per connection, all sharing a single Store
on the loop thread. Store calls are synchronous because they are local and
sub-millisecond; there is no thread pool and no lock, because there is only ever
one thread touching the database.

RPC handlers live in theater/daemon/methods.py — they are registered via the
@method decorator into the METHODS dict, which this module dispatches from.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import socket as _socket

from theater import paths, protocol
from theater.config import Config
from theater.config import load as load_config
from theater.daemon.jobs import JobManager, JobState
from theater.daemon.lock import DaemonLock, file_id
from theater.daemon.observer import Observer
from theater.daemon.registry import Registry
from theater.daemon.spawner import Spawner
from theater.daemon.store import Store
from theater import harness as harness_registry
from theater.harness import Harness
from theater.models import Status, TheaterError
from theater.tmux import client as tmux

# Importing methods registers all @method handlers as a side effect.
from theater.daemon import methods  # noqa: F401
from theater.daemon.methods import METHODS

logger = logging.getLogger("theater.daemon")

#: How often to check whether panes we know about still exist.
REAP_INTERVAL = 1.0

#: sockaddr_un.sun_path is a fixed-size buffer: 104 bytes on macOS/BSD, 108 on
#: Linux. Exceeding it fails with a bare OSError that says nothing useful, so we
#: check first and explain.
MAX_SOCKET_PATH = 100


def _check_socket_path(sock) -> None:
    if len(str(sock).encode()) > MAX_SOCKET_PATH:
        raise RuntimeError(
            f"socket path is too long for the OS ({len(str(sock))} bytes, "
            f"max {MAX_SOCKET_PATH}): {sock}. Set THEATER_HOME to somewhere shorter."
        )


class Daemon:
    def __init__(
        self,
        *,
        store: Store | None = None,
        harnesses: dict[str, Harness] | None = None,
        config: Config | None = None,
    ):
        paths.ensure_home()
        #: Read once, here. There is no reload: see config.py for why, and
        #: `theater restart` for the remedy. Held on the daemon so request
        #: handlers can reach the settings without re-reading the file, which
        #: would let two requests in one process see different values.
        self.config = config if config is not None else load_config()
        # Build the harness registry from the shipped and local plugin
        # directories before anything reads it. Raises ConfigError on anything
        # that cannot be honoured, which is deliberately fatal: the daemon is
        # the process that refuses spawns, so it must not come up holding a set
        # the user did not ask for.
        installed = harness_registry.install(self.config)
        logger.info("harnesses: %s", ", ".join(installed) or "none")
        self.store = store or Store(paths.db_path())
        self.registry = Registry(self.store)
        self.spawner = Spawner(self.registry)
        self.jobs = JobManager(self.store)
        #: `harnesses={}` disables observation entirely, which is what tests
        #: that only exercise the socket want: the real harnesses read the
        #: user's own ~/.claude and ~/.vibe.
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
        #: Held from start() to aclose(). Being the daemon is this lock.
        self._lock = DaemonLock()
        #: (device, inode) of the socket we bound, so shutdown can tell our
        #: own socket from one a successor has since put at the same path.
        self._sock_id: tuple[int, int] | None = None
        self._stopping = asyncio.Event()
        #: One task per open connection. Tracked so shutdown can end them; see
        #: aclose().
        self._conns: set[asyncio.Task] = set()
        #: Monotonic counter for send-job handle uniqueness. Initialized
        #: from the database on start() so a restart does not reuse
        #: handle numbers that already exist in SQLite.
        self._send_seq = 0

    def _next_send_seq(self) -> int:
        self._send_seq += 1
        return self._send_seq

    def _init_send_seq(self) -> None:
        """Initialize the send sequence from the database.

        After a restart, the counter must not reuse handle numbers that
        already exist in the jobs table. `Store.max_send_seq` finds the
        highest one; see its docstring for why the obvious SQL got this
        wrong and handed out duplicate handles.
        """
        try:
            highest = self.store.max_send_seq()
            if highest:
                self._send_seq = highest
                logger.info("send sequence initialized to %d", self._send_seq)
        except Exception as exc:
            logger.debug("could not initialize send sequence: %s", exc)

    # ---- lifecycle -----------------------------------------------------

    async def start(self) -> None:
        """Bind the socket. Raises here, in the caller's face, if it cannot."""
        sock = paths.socket_path()
        _check_socket_path(sock)
        # Lock before touching the socket. Everything after this point is
        # single-threaded across the whole machine, which is what lets
        # _clear_stale_socket unlink without a race.
        self._lock.acquire()
        try:
            self._clear_stale_socket(sock)
            self._server = await asyncio.start_unix_server(
                self._handle, path=str(sock)
            )
            os.chmod(sock, 0o600)
        except BaseException:
            self._lock.release()
            raise
        self._sock_id = file_id(sock)
        await self._reconcile()
        self._init_send_seq()
        self._reaper = asyncio.create_task(self._reap_loop())
        self.observer.start()
        logger.info("listening on %s", sock)

    async def _reconcile(self) -> None:
        """Rebuild in-memory state and reconcile with tmux after a restart.

        SQLite already holds the participants, jobs, and bus. What is lost
        on restart is the in-memory asyncio Events for jobs and the observer
        tasks. This method:
          1. Marks participants whose panes no longer exist as dead.
          2. Crashes running jobs whose targets are dead.
          3. Recreates asyncio Events for jobs that are still running (so
             a re-await can wake up when the job finishes).
        """
        if not tmux.available():
            logger.info("tmux unavailable; skipping reconciliation")
            return
        try:
            out = await tmux.run("list-panes", "-a", "-F", "#{pane_id}", check=False)
            alive_panes = set(out.split())
        except Exception as exc:
            logger.warning("reconcile: could not list panes: %s", exc)
            return

        for p in self.registry.list():
            if p.tmux_pane and p.tmux_pane not in alive_panes:
                if p.status is not Status.DEAD:
                    logger.info("reconcile: %s lost its pane %s", p.id, p.tmux_pane)
                    self.registry.mark_dead(p.id)

        for p in self.registry.list(include_dead=True):
            if p.status is Status.DEAD:
                running = self.store.running_jobs_for_target(p.id)
                for job in running:
                    self.jobs.finish(
                        job.handle, state=JobState.CRASHED, error_code="crashed"
                    )

        for p in self.registry.list():
            if p.status is not Status.DEAD:
                running = self.store.running_jobs_for_target(p.id)
                for job in running:
                    if job.handle not in self.jobs._events:
                        self.jobs._events[job.handle] = asyncio.Event()

        logger.info(
            "reconcile complete: %d participants, %d live panes",
            len(self.registry.list(include_dead=True)),
            len(alive_panes),
        )

    async def serve(self) -> None:
        await self.start()
        assert self._server is not None
        async with self._server:
            await self._stopping.wait()

    def stop(self) -> None:
        self._stopping.set()

    async def aclose(self) -> None:
        self.stop()
        await self.observer.aclose()
        if self._reaper:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
        for task in list(self._conns):
            task.cancel()
        if self._conns:
            await asyncio.gather(*self._conns, return_exceptions=True)
            self._conns.clear()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        self.store.close()
        self._release_files()

    def _release_files(self) -> None:
        """Delete the socket and pidfile — but only if they are still ours.

        A daemon can take seconds to shut down: the observer stops, connections
        drain. A replacement can be listening before that finishes. Unlinking
        by path would then delete the live daemon's socket, and the next client
        would find nothing and start a third. So both deletions are guarded on
        identity, and the socket goes first: once the lock is free, the path it
        protects is already clear.

        Start() may never have run, in which case there is nothing to release
        and both guards fall through.
        """
        sock = paths.socket_path()
        if self._sock_id is not None and file_id(sock) == self._sock_id:
            with contextlib.suppress(OSError):
                sock.unlink()
        self._sock_id = None
        self._lock.release()

    @staticmethod
    def _clear_stale_socket(sock) -> None:
        """Remove a socket left behind by a daemon that did not shut down.

        Called while holding the lock, so nothing can bind between the probe
        and the unlink. A socket that still answers therefore means a daemon
        from before the lock existed: refuse rather than steal its socket, and
        let the user stop it. Without that check an upgrade would take the
        socket away from the running old daemon.
        """
        if not sock.exists():
            return
        probe = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        probe.settimeout(0.25)
        try:
            probe.connect(str(sock))
        except OSError:
            sock.unlink()
            return
        finally:
            probe.close()
        raise RuntimeError(f"a theater daemon is already listening on {sock}")

    # ---- reaper --------------------------------------------------------

    async def _reap_loop(self) -> None:
        """Mark participants dead once their pane is gone.

        Polling, not tmux hooks. A hook would be cheaper but would make the
        daemon's correctness depend on state living inside the user's tmux
        config, which survives neither `tmux kill-server` nor a config reload.
        """
        while not self._stopping.is_set():
            try:
                await self._reap_once()
            except Exception:
                logger.exception("reaper iteration failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=REAP_INTERVAL)

    async def _reap_once(self) -> None:
        tracked = [p for p in self.registry.list() if p.tmux_pane]
        if not tracked:
            return
        if not tmux.available():
            return
        out = await tmux.run("list-panes", "-a", "-F", "#{pane_id}", check=False)
        alive = set(out.split())
        for p in tracked:
            if p.tmux_pane not in alive:
                logger.info("participant %s lost its pane %s", p.id, p.tmux_pane)
                self.registry.mark_dead(p.id)
                running = self.store.running_jobs_for_target(p.id)
                for job in running:
                    self.jobs.finish(
                        job.handle, state=JobState.CRASHED, error_code="crashed"
                    )

    # ---- connection handling -------------------------------------------

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._conns.add(task)
        try:
            while line := await reader.readline():
                response = await self._dispatch(line)
                writer.write(response)
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            if task is not None:
                self._conns.discard(task)
            writer.close()
            with contextlib.suppress(BaseException):
                await writer.wait_closed()

    async def _dispatch(self, line: bytes) -> bytes:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            return protocol.err(0, "bad_request", f"malformed json: {exc}")

        req_id = msg.get("id", 0)
        name = msg.get("method")
        params = msg.get("params") or {}
        handler = METHODS.get(name)
        if handler is None:
            return protocol.err(req_id, "unknown_method", f"no method {name!r}")

        try:
            result = await handler(self, params)
        except TheaterError as exc:
            return protocol.err(req_id, exc.code, str(exc))
        except Exception as exc:
            logger.exception("handler %s failed", name)
            return protocol.err(req_id, "internal", f"{type(exc).__name__}: {exc}")

        return protocol.ok(req_id, result)


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
        await daemon.aclose()
