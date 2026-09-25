"""Daemon socket client for CLI and MCP; the first client finding no socket autostarts the daemon.
One reused connection: replies are id-checked, abandoned/overlong reads poison it (partial lines),
and failed calls are never retried (a resend would duplicate a prompt).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import subprocess
import sys
from pathlib import Path

from theater import paths, protocol
from theater.constants.daemon import RPC_DEFAULT_MAX_WAIT_SECONDS, RPC_MAX_AWAIT_SECONDS
from theater.observability.correlation import CALL_ID_KEY, current_call_id
from theater.observability.engine import span as timing_span
from theater.protocol import RemoteError

#: How long to wait for a freshly started daemon to come up.
START_TIMEOUT = 8.0

#: Private request timeout; physical terminal callbacks own their own deadline.
CALL_TIMEOUT = 40.0


class DaemonClient:
    def __init__(self, *, autostart: bool = True):
        self.autostart = autostart
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._next_id = 0
        self._lock = asyncio.Lock()
        #: The exact stderr generation path from the last autostart, or None.
        self._stderr_path: Path | None = None

    async def connect(self) -> None:
        """Open the connection if we do not have one; lazy reconnect heals poisoned connections."""
        if self._writer is not None:
            return
        sock = paths.socket_path()
        try:
            self._reader, self._writer = await asyncio.open_unix_connection(
                str(sock), limit=protocol.MAX_MESSAGE_BYTES
            )
        except (FileNotFoundError, ConnectionRefusedError):
            if not self.autostart:
                raise
        else:
            return
        self._stderr_path = await self._start_daemon()
        self._reader, self._writer = await self._await_socket()

    async def _start_daemon(self) -> Path | None:
        """Launch a detached daemon unless one is already coming up; return its stderr path or None.
        The lock check only suppresses a cold-start herd that would push the winner past the connect
        timeout; the daemon lock guarantees the singleton, so the race here is harmless.
        """
        from theater.daemon import lock

        if not lock.is_free():
            return None
        paths.ensure_home()
        from theater.constants.observability import STDERR_TOKEN_RETRIES
        from theater.observability.logging import create_generation_file, delete_generation_file

        path, token, fd = create_generation_file(
            paths.daemon_stderr_logs_dir(), retries=STDERR_TOKEN_RETRIES
        )
        child_stderr = os.fdopen(fd, "wb", closefd=True)
        popened = False
        try:
            subprocess.Popen(  # noqa: ASYNC220
                [
                    sys.executable,
                    "-m",
                    "theater.cli",
                    "daemon",
                    "--stderr-token",
                    token,
                ],
                stdout=child_stderr,
                stderr=child_stderr,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=os.environ.copy(),
            )
            popened = True
        finally:
            child_stderr.close()
            if not popened:
                delete_generation_file(path)
        return path

    async def _await_socket(self):
        sock = paths.socket_path()
        deadline = asyncio.get_running_loop().time() + START_TIMEOUT
        delay = 0.02
        last: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                return await asyncio.open_unix_connection(
                    str(sock), limit=protocol.MAX_MESSAGE_BYTES
                )
            except (FileNotFoundError, ConnectionRefusedError) as exc:
                last = exc
                await asyncio.sleep(delay)
                delay = min(delay * 1.6, 0.25)
        # Name the exact spawned generation when known; otherwise safe generic paths.
        if self._stderr_path is not None:
            raise ConnectionError(
                f"daemon did not come up within {START_TIMEOUT}s; see {self._stderr_path}"
            ) from last
        raise ConnectionError(
            f"daemon did not come up within {START_TIMEOUT}s; see {paths.log_path()} "
            f"or {paths.daemon_stderr_logs_dir() / '*.log'}"
        ) from last

    @staticmethod
    def _timeout_for(method: str, params: dict) -> float:
        """Read timeout for one method: ``jobs.await`` blocks by design, so it gets max_wait plus
        slack.
        """
        if method == "jobs.await":
            return DaemonClient._await_timeout(params)
        if method == "plugin.call" and params.get("operation") == "jobs.await":
            nested = params.get("params")
            if isinstance(nested, dict):
                return DaemonClient._await_timeout(nested)
        return CALL_TIMEOUT

    @staticmethod
    def _await_timeout(params: dict) -> float:
        """Derive a bounded reply budget for an await-shaped parameter object."""
        raw = params.get("max_wait", RPC_DEFAULT_MAX_WAIT_SECONDS)
        if isinstance(raw, bool):
            return CALL_TIMEOUT
        try:
            wait = float(raw)
        except (OverflowError, TypeError, ValueError):
            return CALL_TIMEOUT
        if not math.isfinite(wait):
            return CALL_TIMEOUT
        return min(max(wait, 0.0), RPC_MAX_AWAIT_SECONDS) + CALL_TIMEOUT

    async def call(self, method: str, **params) -> object:
        """A failed call is never retried, by design: the side effect may have landed,
        and even reads mutate (jobs.await, trajectory snapshot/close); safe retries
        would need durable daemon-side idempotency keys and a fault-injection audit.
        """
        from theater.observability.catalog import RPC_CLIENT

        with timing_span(
            RPC_CLIENT,
            method=method,
            call_id=current_call_id(),
            slow_ms=float("inf") if method == "jobs.await" else None,
        ) as fields:
            with fields.measure("lock_wait_ms"):
                await self._lock.acquire()
            try:
                with fields.measure("connect_ms"):
                    await self.connect()
                self._next_id += 1
                fields["request_id"] = self._next_id
                with fields.measure("roundtrip_ms"):
                    return await self._exchange(self._next_id, method, params)
            finally:
                self._lock.release()

    async def _exchange(self, req_id: int, method: str, params: dict) -> object:
        """One aligned exchange; an abandoned read still poisons its connection."""
        from theater.observability.tracing import inject_trace_context

        assert self._reader and self._writer
        try:
            meta = inject_trace_context()
            if call_id := current_call_id():
                meta[CALL_ID_KEY] = call_id
            self._writer.write(
                protocol.request(req_id, method, params, meta=meta if meta else None)
            )
            await self._writer.drain()
            msg = await self._read_reply(req_id, self._timeout_for(method, params))
        except asyncio.CancelledError:
            self._discard()
            raise
        except (TimeoutError, ConnectionError, OSError):
            await self._drop()
            raise
        if not msg.get("ok"):
            error = msg.get("error") or {}
            details = error.get("details")
            raise RemoteError(
                error.get("code", "error"),
                error.get("message", ""),
                details if isinstance(details, dict) else None,
            )
        return msg.get("result")

    async def _read_reply(self, req_id: int, timeout: float) -> dict:
        """Read until the reply to req_id arrives, or the budget runs out.
        Lower ids are leftovers of abandoned calls and are skipped (defence in depth); a higher id
        means the daemon answers something never asked, which is unrecoverable.
        """
        assert self._reader is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"no reply to request {req_id} within {timeout}s")
            line = await asyncio.wait_for(protocol.read_message(self._reader), timeout=remaining)
            if not line:
                raise ConnectionError("daemon closed the connection")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                # Half a line left by a cancelled read — unrecoverable, raise as a connection fault.
                raise ConnectionError(f"truncated reply from daemon: {exc}") from exc
            if not isinstance(msg, dict):
                raise ConnectionError("daemon sent a non-object reply")
            got = msg.get("id")
            # The daemon answers with id 0 when it could not parse the request to echo one.
            if got in (req_id, 0):
                return msg
            if isinstance(got, int) and got < req_id:
                continue
            raise ConnectionError(f"daemon replied to request {got!r} while {req_id} was in flight")

    def _discard(self) -> None:
        """Forget the connection without waiting for the close to complete."""
        writer, self._reader, self._writer = self._writer, None, None
        if writer is not None:
            writer.close()

    async def _drop(self) -> None:
        writer = self._writer
        self._discard()
        if writer is not None:
            with contextlib.suppress(Exception):
                await writer.wait_closed()

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
