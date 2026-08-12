"""Client side of the daemon socket, used by both the CLI and the MCP server.

Auto-start is the point of this module. An agent's MCP server has no way to ask
a human to run `theater daemon` first, so the first client to find the socket
missing starts one and waits for it. Concurrent starters are harmless: the
daemon refuses to bind a socket another daemon is already listening on, so the
loser exits and the winner serves both.

One connection is reused for every call, which makes reply/request alignment a
correctness problem rather than a nicety. If a read is abandoned -- a timeout,
a cancelled task -- the reply the daemon eventually writes stays in the socket
buffer, and the *next* call reads it as its own answer. Every later call is
then off by one, silently, for the life of the process; an MCP server builds
one client per agent session, so one slow call used to poison every tool call
that agent made afterwards. Three rules keep that from happening:

* every reply is checked against the id of the request that is in flight;
* an abandoned read poisons the connection, because a cancelled ``readline``
  can leave a *partial* line behind that no id check can undo;
* a poisoned connection reconnects lazily on the next call, and the call that
  failed is not retried -- ``send`` and ``spawn`` type into a live pane, so a
  transparent resend would duplicate a prompt.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

from theater import paths, protocol
from theater.protocol import RemoteError
from theater.tmux import client as tmux

#: How long to wait for a freshly started daemon to come up.
START_TIMEOUT = 8.0

#: How long to wait for a reply before declaring the connection unusable.
#: Derived from the tmux ceiling rather than picked: the slowest handlers are
#: the ones that shell out to tmux (``send`` runs up to three invocations --
#: presence check, literal keys, Enter), so a client that gave up first would
#: abandon a read that the daemon was still going to answer. Deriving it keeps
#: the two numbers linked; a flat constant drifted to 5s and caused exactly
#: that. A per-method table was rejected -- it rots as methods are added.
CALL_TIMEOUT = 4 * tmux.RUN_TIMEOUT


class DaemonClient:
    def __init__(self, *, autostart: bool = True):
        self.autostart = autostart
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._next_id = 0
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """Open the connection if we do not have one.

        Reconnection is lazy and lives here, so a dropped or poisoned
        connection heals on the next call instead of raising forever.
        """
        if self._writer is not None:
            return
        sock = paths.socket_path()
        try:
            self._reader, self._writer = await asyncio.open_unix_connection(str(sock))
            return
        except (FileNotFoundError, ConnectionRefusedError):
            if not self.autostart:
                raise
        await self._start_daemon()
        self._reader, self._writer = await self._await_socket()

    async def _start_daemon(self) -> None:
        """Launch a detached daemon.

        start_new_session detaches it from our process group so that killing the
        agent that happened to start it does not take the daemon with it.
        """
        paths.ensure_home()
        log = open(paths.log_path(), "ab")
        subprocess.Popen(
            [sys.executable, "-m", "theater.cli", "daemon"],
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=os.environ.copy(),
        )

    async def _await_socket(self):
        sock = paths.socket_path()
        deadline = asyncio.get_running_loop().time() + START_TIMEOUT
        delay = 0.02
        last: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                return await asyncio.open_unix_connection(str(sock))
            except (FileNotFoundError, ConnectionRefusedError) as exc:
                last = exc
                await asyncio.sleep(delay)
                delay = min(delay * 1.6, 0.25)
        raise ConnectionError(
            f"daemon did not come up within {START_TIMEOUT}s; see {paths.log_path()}"
        ) from last

    @staticmethod
    def _timeout_for(method: str, params: dict) -> float:
        """Read timeout for one method.

        jobs.await blocks on purpose for up to max_wait, so it gets its own
        budget plus slack; everything else shares CALL_TIMEOUT.
        """
        if method == "jobs.await":
            return float(params.get("max_wait", 60.0)) + CALL_TIMEOUT
        return CALL_TIMEOUT

    async def call(self, method: str, **params) -> object:
        # The lock covers connect() too: two callers racing on a poisoned
        # connection would otherwise each open one and orphan the loser.
        async with self._lock:
            await self.connect()
            assert self._reader and self._writer
            self._next_id += 1
            req_id = self._next_id
            try:
                self._writer.write(protocol.request(req_id, method, params))
                await self._writer.drain()
                msg = await self._read_reply(req_id, self._timeout_for(method, params))
            except asyncio.CancelledError:
                # Awaiting during cancellation is not safe, so tear the
                # connection down without waiting for the close to land.
                self._discard()
                raise
            except (TimeoutError, ConnectionError, OSError):
                await self._drop()
                raise
        if not msg.get("ok"):
            error = msg.get("error") or {}
            raise RemoteError(error.get("code", "error"), error.get("message", ""))
        return msg.get("result")

    async def _read_reply(self, req_id: int, timeout: float) -> dict:
        """Read until the reply to req_id arrives, or the budget runs out.

        Ids only ever grow, and the lock allows one call in flight, so a reply
        numbered below the request we are waiting on is the leftover of a call
        that gave up: drop it and keep reading. This is defence in depth --
        an abandoned read normally poisons the connection -- but it is what
        rescues a connection whose reader was cancelled rather than timed out.
        A reply numbered *above* it means the daemon is answering things we
        never asked, which no amount of skipping can repair.
        """
        assert self._reader is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"no reply to request {req_id} within {timeout}s")
            line = await asyncio.wait_for(self._reader.readline(), timeout=remaining)
            if not line:
                raise ConnectionError("daemon closed the connection")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                # Half a line: the tail of a reply whose head someone else
                # already read. Unrecoverable in place -- raise as a
                # connection fault so the caller drops and reconnects.
                raise ConnectionError(f"truncated reply from daemon: {exc}") from exc
            got = msg.get("id")
            # The daemon answers with id 0 when it could not parse the request
            # far enough to echo one back; that is still our reply.
            if got == req_id or got == 0:
                return msg
            if isinstance(got, int) and got < req_id:
                continue
            raise ConnectionError(
                f"daemon replied to request {got!r} while {req_id} was in flight"
            )

    def _discard(self) -> None:
        """Forget the connection without waiting for the close to complete."""
        writer, self._reader, self._writer = self._writer, None, None
        if writer is not None:
            writer.close()

    async def _drop(self) -> None:
        writer = self._writer
        self._discard()
        if writer is not None:
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def aclose(self) -> None:
        await self._drop()

    async def __aenter__(self) -> DaemonClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()


def call_sync(method: str, **params) -> object:
    """One-shot call for the CLI, which has no event loop of its own."""

    async def go():
        async with DaemonClient() as client:
            return await client.call(method, **params)

    return asyncio.run(go())
