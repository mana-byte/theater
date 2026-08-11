"""The daemon: one per machine, owner of the registry.

Singleton by construction. The socket path is fixed under THEATER_HOME and a
lockfile holds the pid, so a second `theater daemon` exits rather than racing
the first for the same SQLite file.

Concurrency model: one asyncio task per connection, all sharing a single Store
on the loop thread. Store calls are synchronous because they are local and
sub-millisecond; there is no thread pool and no lock, because there is only ever
one thread touching the database.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
from typing import Any, Awaitable, Callable

from theater import paths, protocol
from theater.daemon.observer import Observer
from theater.daemon.registry import Registry
from theater.daemon.spawner import SpawnRequest, Spawner
from theater.daemon.store import Store
from theater.harness import Harness
from theater.models import BadRequest, Status, TheaterError
from theater.tmux import client as tmux

logger = logging.getLogger("theater.daemon")

#: How often to check whether panes we know about still exist.
REAP_INTERVAL = 1.0

#: sockaddr_un.sun_path is a fixed-size buffer: 104 bytes on macOS/BSD, 108 on
#: Linux. Exceeding it fails with a bare OSError that says nothing useful, so we
#: check first and explain.
MAX_SOCKET_PATH = 100

Handler = Callable[["Daemon", dict[str, Any]], Awaitable[Any]]
_METHODS: dict[str, Handler] = {}


def _check_socket_path(sock) -> None:
    if len(str(sock).encode()) > MAX_SOCKET_PATH:
        raise RuntimeError(
            f"socket path is too long for the OS ({len(str(sock))} bytes, "
            f"max {MAX_SOCKET_PATH}): {sock}. Set THEATER_HOME to somewhere shorter."
        )


def method(name: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        _METHODS[name] = fn
        return fn

    return register


class Daemon:
    def __init__(
        self,
        *,
        store: Store | None = None,
        harnesses: dict[str, Harness] | None = None,
    ):
        paths.ensure_home()
        self.store = store or Store(paths.db_path())
        self.registry = Registry(self.store)
        self.spawner = Spawner(self.registry)
        #: `harnesses={}` disables observation entirely, which is what tests
        #: that only exercise the socket want: the real harnesses read the
        #: user's own ~/.claude and ~/.vibe.
        self.observer = Observer(self.registry, harnesses)
        self._server: asyncio.Server | None = None
        self._reaper: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        #: One task per open connection. Tracked so shutdown can end them; see
        #: aclose().
        self._conns: set[asyncio.Task] = set()

    # ---- lifecycle -----------------------------------------------------

    async def start(self) -> None:
        """Bind the socket. Raises here, in the caller's face, if it cannot."""
        sock = paths.socket_path()
        _check_socket_path(sock)
        self._clear_stale_socket(sock)
        self._server = await asyncio.start_unix_server(self._handle, path=str(sock))
        os.chmod(sock, 0o600)
        self._write_pidfile()
        self._reaper = asyncio.create_task(self._reap_loop())
        self.observer.start()
        logger.info("listening on %s", sock)

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
        # Every connected agent parks a handler on readline() for the lifetime of
        # its MCP server. Server.wait_closed() waits for those handlers to
        # return, so without cancelling them first a daemon with even one live
        # participant could never exit — `theater stop` would hang until the
        # last agent quit. Shutdown is not a negotiation: end them.
        for task in list(self._conns):
            task.cancel()
        if self._conns:
            await asyncio.gather(*self._conns, return_exceptions=True)
            self._conns.clear()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        self.store.close()
        with contextlib.suppress(FileNotFoundError):
            paths.socket_path().unlink()
        with contextlib.suppress(FileNotFoundError):
            self._pidfile().unlink()

    def _pidfile(self):
        return paths.home() / "daemon.pid"

    def _write_pidfile(self) -> None:
        self._pidfile().write_text(str(os.getpid()))

    @staticmethod
    def _clear_stale_socket(sock) -> None:
        """Remove a socket left behind by a daemon that did not shut down.

        Probing it first: a live daemon accepts the connection, in which case we
        must not delete its socket out from under it.
        """
        if not sock.exists():
            return
        import socket as _socket

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
            # Cancellation lands here during shutdown; wait_closed() would just
            # raise it again, and the transport is going away regardless.
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
        handler = _METHODS.get(name)
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


# ---- methods -----------------------------------------------------------


def _require(params: dict, key: str) -> Any:
    if key not in params or params[key] in (None, ""):
        raise BadRequest(f"missing required parameter {key!r}")
    return params[key]


@method("ping")
async def _ping(daemon: Daemon, params: dict) -> dict:
    return {"pong": True, "protocol": protocol.PROTOCOL_VERSION}


@method("hello")
async def _hello(daemon: Daemon, params: dict) -> dict:
    """First contact. Establishes or confirms the caller's identity and tier."""
    participant = daemon.registry.register(
        harness=params.get("harness") or "unknown",
        pane=params.get("pane"),
        cwd=params.get("cwd"),
        session_id=params.get("session_id"),
        claimed_id=params.get("id"),
    )
    return participant.to_dict()


@method("participants.list")
async def _list(daemon: Daemon, params: dict) -> list[dict]:
    include_dead = bool(params.get("include_dead"))
    return [p.to_dict() for p in daemon.registry.list(include_dead=include_dead)]


@method("participants.tree")
async def _tree(daemon: Daemon, params: dict) -> list[dict]:
    return daemon.registry.tree()


@method("participants.get")
async def _get(daemon: Daemon, params: dict) -> dict:
    return daemon.registry.get(_require(params, "id")).to_dict()


@method("participant.status")
async def _status(daemon: Daemon, params: dict) -> dict:
    pid = _require(params, "id")
    raw = _require(params, "status")
    try:
        status = Status(raw)
    except ValueError:
        raise BadRequest(f"unknown status {raw!r}") from None
    daemon.registry.set_status(pid, status)
    return daemon.registry.get(pid).to_dict()


@method("participant.kill")
async def _kill(daemon: Daemon, params: dict) -> dict:
    pid = _require(params, "id")
    await daemon.spawner.kill(pid)
    return {"id": pid, "killed": True}


@method("spawn")
async def _spawn(daemon: Daemon, params: dict) -> dict:
    req = SpawnRequest(
        harness=_require(params, "harness"),
        prompt=params.get("prompt") or "",
        cwd=_require(params, "cwd"),
        approval=_require(params, "approval"),
        parent_id=params.get("parent_id"),
        tmux_session=params.get("tmux_session"),
        window_name=params.get("window_name"),
        background=params.get("background", True),
    )
    participant = await daemon.spawner.spawn(req)
    return participant.to_dict()


@method("bus.tail")
async def _bus_tail(daemon: Daemon, params: dict) -> list[dict]:
    return daemon.store.bus_tail(
        limit=int(params.get("limit", 100)), after_id=int(params.get("after_id", 0))
    )


@method("shutdown")
async def _shutdown(daemon: Daemon, params: dict) -> dict:
    daemon.stop()
    return {"stopping": True}


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
