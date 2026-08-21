"""Socket transport: path validation, stale-socket clearing, and connection dispatch.

Separated from server.py so the transport concerns are testable independently
of lifecycle and maintenance. The Daemon owns the asyncio.Server; this module
provides the helpers and the per-connection handler that the server calls.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import socket as _socket

from theater import protocol, timing
from theater.daemon.rpc import METHODS
from theater.models import TheaterError

logger = logging.getLogger("theater.daemon")

#: sockaddr_un.sun_path is a fixed-size buffer: 104 on macOS/BSD, 108 on Linux.
MAX_SOCKET_PATH = 100


def check_socket_path(sock) -> None:
    """Raise if the unix socket path exceeds the OS buffer."""
    if len(str(sock).encode()) > MAX_SOCKET_PATH:
        raise RuntimeError(
            f"socket path is too long for the OS ({len(str(sock))} bytes, "
            f"max {MAX_SOCKET_PATH}): {sock}. Set THEATER_HOME to somewhere shorter."
        )


def clear_stale_socket(sock) -> None:
    """Remove a socket left behind by a daemon that did not shut down.

    Called while holding the lock, so nothing can bind between the probe and
    the unlink. A socket that still answers means a daemon from before the
    lock existed: refuse rather than steal its socket.
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


async def handle_connection(daemon, reader, writer) -> None:
    """Per-connection handler: read-dispatch-write until the client disconnects."""
    task = __import__("asyncio").current_task()
    if task is not None:
        daemon._conns.add(task)
    try:
        while True:
            try:
                line = await protocol.read_message(reader)
            except protocol.MessageTooLarge as exc:
                # Answer rather than hang up: one absurd prompt should not cost
                # an agent its session. id 0 means "could not read far enough".
                logger.warning("oversized request: %s", exc)
                writer.write(protocol.err(0, "too_large", str(exc)))
                await writer.drain()
                await protocol.drain_message(reader)
                continue
            if not line:
                break
            response = await dispatch(daemon, line)
            writer.write(response)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        if task is not None:
            daemon._conns.discard(task)
        writer.close()
        with contextlib.suppress(BaseException):
            await writer.wait_closed()


async def dispatch(daemon, line: bytes) -> bytes:
    """Parse one NDJSON request, call the handler, and return a response."""
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

    # `jobs.await` is exempt because blocking is what it is *for*.
    slow_ms = math.inf if name == "jobs.await" else timing.DEFAULT_SLOW_MS
    try:
        with timing.span(f"rpc.{name}", slow_ms=slow_ms, caller=params.get("caller_id")):
            result = await handler(daemon, params)
    except TheaterError as exc:
        return protocol.err(req_id, exc.code, str(exc))
    except Exception as exc:
        logger.exception("handler %s failed", name)
        return protocol.err(req_id, "internal", f"{type(exc).__name__}: {exc}")

    return protocol.ok(req_id, result)
