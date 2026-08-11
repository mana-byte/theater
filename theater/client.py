"""Client side of the daemon socket, used by both the CLI and the MCP server.

Auto-start is the point of this module. An agent's MCP server has no way to ask
a human to run `theater daemon` first, so the first client to find the socket
missing starts one and waits for it. Concurrent starters are harmless: the
daemon refuses to bind a socket another daemon is already listening on, so the
loser exits and the winner serves both.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

from theater import paths, protocol
from theater.protocol import RemoteError

#: How long to wait for a freshly started daemon to come up.
START_TIMEOUT = 8.0
CONNECT_TIMEOUT = 5.0


class DaemonClient:
    def __init__(self, *, autostart: bool = True):
        self.autostart = autostart
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._next_id = 0
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
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

    async def call(self, method: str, **params) -> object:
        await self.connect()
        assert self._reader and self._writer
        async with self._lock:
            self._next_id += 1
            req_id = self._next_id
            self._writer.write(protocol.request(req_id, method, params))
            await self._writer.drain()
            line = await asyncio.wait_for(
                self._reader.readline(), timeout=CONNECT_TIMEOUT
            )
        if not line:
            raise ConnectionError("daemon closed the connection")
        msg = json.loads(line)
        if not msg.get("ok"):
            error = msg.get("error") or {}
            raise RemoteError(error.get("code", "error"), error.get("message", ""))
        return msg.get("result")

    async def aclose(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
            self._reader = self._writer = None

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
