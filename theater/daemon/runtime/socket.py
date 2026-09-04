"""Socket transport: path validation, stale-socket clearing, and connection dispatch.

Separated from server.py so the transport concerns are testable independently
of lifecycle and maintenance. The Daemon owns the asyncio.Server; this module
provides the helpers and the per-connection handler that the server calls.
"""

from __future__ import annotations

import contextlib
import json
import logging
import socket as _socket
from typing import Any

from theater import protocol, timing
from theater.models import TheaterError
from theater.observability.catalog import RPC_AWAIT, RPC_SERVER
from theater.observability.tracing import extract_trace_context

logger = logging.getLogger("theater.daemon")

#: sockaddr_un.sun_path is a fixed-size buffer: 104 on macOS/BSD, 108 on Linux.
MAX_SOCKET_PATH = 100


def check_socket_path(sock, *, maximum: int = MAX_SOCKET_PATH) -> None:
    """Raise if the unix socket path exceeds the OS buffer."""
    if len(str(sock).encode()) > maximum:
        raise RuntimeError(
            f"socket path is too long for the OS ({len(str(sock))} bytes, "
            f"max {maximum}): {sock}. Set THEATER_HOME to somewhere shorter."
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
                # Answer with id 0 when the request was too large to read its real id.
                logger.warning("oversized request: %s", exc)
                writer.write(protocol.err(0, "too_large", str(exc)))
                await writer.drain()
                await protocol.drain_message(reader)
                continue
            if not line:
                break
            response = await daemon._dispatch(line)
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


async def dispatch(daemon, line: bytes, *, methods) -> bytes:
    """Parse one NDJSON request, call the handler, and return a response."""
    try:
        msg = json.loads(line)
    except json.JSONDecodeError as exc:
        return protocol.err(0, "bad_request", f"malformed json: {exc}")

    req_id = msg.get("id", 0)
    name = msg.get("method")
    params = msg.get("params") or {}
    handler = methods.get(name)
    if handler is None:
        return protocol.err(req_id, "unknown_method", f"no method {name!r}")

    parent_context = extract_trace_context(msg.get("_meta"))

    spec = RPC_AWAIT if name == "jobs.await" else RPC_SERVER
    fields: dict[str, Any] = {"caller": params.get("caller_id")}
    if spec.key == "RPC_SERVER":
        fields["method"] = name
    error: tuple[str, str, dict[str, Any] | None] | None
    with timing.span(spec, parent_context=parent_context, **fields) as sp:
        try:
            result = await handler(daemon, params)
        except TheaterError as exc:
            sp.set_result("error", error_type=exc.code)
            details = getattr(exc, "details", None)
            error = (exc.code, str(exc), details if isinstance(details, dict) else None)
        except Exception as exc:
            et = f"{type(exc).__module__}.{type(exc).__qualname__}"
            sp.set_result("error", error_type=et)
            logger.exception("handler %s failed", name)
            error = ("internal", f"{type(exc).__name__}: {exc}", None)
        else:
            error = None

    if error is not None:
        return protocol.err(req_id, error[0], error[1], details=error[2])
    return protocol.ok(req_id, result)
