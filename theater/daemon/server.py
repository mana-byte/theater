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
from theater.daemon.jobs import JobManager, JobState
from theater.daemon.observer import Observer
from theater.daemon.rails import check_budget, check_cycle, check_depth
from theater.daemon.registry import Registry
from theater.daemon.spawner import SpawnRequest, Spawner
from theater.daemon.store import Store
from theater.harness import Harness, known_binaries, normalize
from theater.models import BadRequest, Status, TheaterError
from theater.tmux import client as tmux

import subprocess

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
        self.jobs = JobManager(self.store)
        #: `harnesses={}` disables observation entirely, which is what tests
        #: that only exercise the socket want: the real harnesses read the
        #: user's own ~/.claude and ~/.vibe.
        self.observer = Observer(self.registry, harnesses, jobs=self.jobs)
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
                # Crash any running jobs for this participant.
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


@method("adopt")
async def _adopt(daemon: Daemon, params: dict) -> dict:
    """Adopt a pane the user is already running a harness in.

    The CLI reads $TMUX_PANE and sends it here; the daemon does the tmux lookup
    to learn the pane's current command (the binary name) and its current path
    (the cwd), maps the binary to a harness, and registers the participant.

    If the pane's command does not match a known harness, the caller may have
    passed --harness to override; if neither yields a known harness, the
    participant is registered as `unknown` and will be unobservable — better
    than refusing, because the pane is real either way.
    """
    pane = _require(params, "pane")
    override = params.get("harness")
    cwd = params.get("cwd")
    if not tmux.available():
        raise BadRequest("tmux is not available; cannot look up pane")
    panes = await tmux.list_panes()
    match = next((p for p in panes if p.pane_id == pane), None)
    if match is None:
        raise BadRequest(f"pane {pane!r} not found in tmux")
    harness = normalize(override) if override else _detect_harness(match.current_command, match.pane_pid)
    if cwd is None:
        cwd = match.cwd
    participant = daemon.registry.register(
        harness=harness,
        pane=pane,
        cwd=cwd,
    )
    return participant.to_dict()


def _detect_harness(pane_command: str, pane_pid: int) -> str:
    """Map a pane to a canonical harness name, or 'unknown'.

    `pane_current_command` is the instantaneous foreground process — which,
    when `theater adopt` is the thing running, is `theater`/`uv`/`python3`,
    not the harness session that is its ancestor in the process tree.

    So we first check the foreground command (the common case when no adopt
    is in flight), then walk the process tree from the pane's shell pid
    looking for any descendant whose name matches a known harness binary.
    The pane's shell spawned `vibe`, which spawned the bash tool running
    `theater adopt` — so `vibe` is in the tree even though it is not the
    foreground leaf.
    """
    from theater.harness import HARNESSES

    name = _match_binary(pane_command, HARNESSES)
    if name:
        return name
    for comm in _descendant_comms(pane_pid):
        name = _match_binary(comm, HARNESSES)
        if name:
            return name
    return "unknown"


def _match_binary(command: str, harnesses) -> str | None:
    """Return the harness name if a command basename matches a harness binary."""
    basename = command.rsplit("/", 1)[-1]
    for harness in harnesses.values():
        if harness.binary == basename or harness.binary == command:
            return harness.name
    return None


def _descendant_comms(root_pid: int) -> list[str]:
    """Process names of root_pid and all its descendants, breadth-first.

    Uses `ps` rather than /proc or psutil to stay dependency-free. The pane's
    shell spawned `vibe`, which spawned the bash tool running `theater adopt`
    — so `vibe` is in the tree even though it is not the foreground leaf.
    """
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid,ppid,comm"],
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    # Build ppid -> [(pid, comm)] map.
    pid_children: dict[int, list[tuple[int, str]]] = {}
    for line in out.strip().splitlines()[1:]:  # skip header
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        comm = parts[2]
        pid_children.setdefault(ppid, []).append((pid, comm))
    # BFS from root_pid, collecting comm names.
    result: list[str] = []
    queue = [root_pid]
    while queue:
        pid = queue.pop(0)
        for child_pid, comm in pid_children.get(pid, []):
            result.append(comm)
            queue.append(child_pid)
    return result


@method("participants.unmanaged")
async def _unmanaged(daemon: Daemon, params: dict) -> list[dict]:
    """Panes running a known harness binary with no participant record.

    These are hand-started agent sessions Theater does not yet know about.
    Surfaced in `theater ls` as unmanaged rather than invisible, because a tree
    that silently omits half the agents on the machine is worse than one that
    admits ignorance.
    """
    if not tmux.available():
        return []
    panes = await tmux.list_panes()
    registered = {p.tmux_pane for p in daemon.registry.list() if p.tmux_pane}
    out: list[dict] = []
    for p in panes:
        if p.pane_id in registered:
            continue
        # Check the foreground command first, then walk the process tree.
        # A pane running `vibe` via `uv run` shows `python3` as the
        # foreground, but `vibe` is its parent in the process tree.
        harness = _detect_harness(p.current_command, p.pane_pid)
        if harness != "unknown":
            out.append(
                {
                    "pane": p.pane_id,
                    "command": p.current_command,
                    "harness": harness,
                    "cwd": p.cwd,
                    "session": p.session,
                    "window_name": p.window_name,
                }
            )
    return out


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
    # Safety rails: reject before creating anything.
    check_depth(daemon.store, req.parent_id)
    check_budget(daemon.store, req.parent_id)

    participant = await daemon.spawner.spawn(req)
    # Create a job for this spawn so the caller can await the result.
    handle = participant.id  # the handle is the participant id itself.
    daemon.jobs.create(
        handle=handle,
        caller_id=params.get("parent_id") or "cli",
        target_id=participant.id,
        kind="spawn",
        prompt=req.prompt or "",
    )
    result = participant.to_dict()
    result["handle"] = handle
    return result


@method("jobs.await")
async def _jobs_await(daemon: Daemon, params: dict) -> list[dict]:
    """Wait for one or more jobs to finish, up to max_wait seconds.

    Returns the current state of each job. A job that is still running when
    the timeout expires is returned with state="running" — the caller should
    re-await if it wants to keep waiting.
    """
    handles = params.get("handles") or []
    if not handles:
        raise BadRequest("at least one handle is required")
    max_wait = float(params.get("max_wait", 60.0))
    # Cycle detection: reject if the caller appears in the await chain
    # of any target.
    caller_id = params.get("caller_id")
    if caller_id:
        check_cycle(daemon.store, caller_id, handles)
    jobs = await daemon.jobs.await_jobs(handles, max_wait=max_wait)
    return [j.to_dict() for j in jobs]


@method("jobs.status")
async def _jobs_status(daemon: Daemon, params: dict) -> dict:
    """Get the current state of a single job."""
    handle = _require(params, "handle")
    job = daemon.jobs.get(handle)
    if job is None:
        raise BadRequest(f"no job {handle!r}")
    return job.to_dict()


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
