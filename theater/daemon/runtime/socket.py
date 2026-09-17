"""Socket transport: path validation, stale-socket clearing, and connection dispatch.

Separated from server.py so the transport concerns are testable independently
of lifecycle and maintenance. The Daemon owns the asyncio.Server; this module
provides the helpers and the per-connection handler that the server calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import json
import logging
import os
import socket as _socket
import struct
import sys
from collections.abc import Mapping
from typing import Any

from theater import protocol, timing
from theater.frontend.capabilities import PUBLIC_LIMITS
from theater.models import TheaterError
from theater.observability.catalog import RPC_AWAIT, RPC_SERVER
from theater.observability.tracing import extract_trace_context

logger = logging.getLogger("theater.daemon")

#: sockaddr_un.sun_path is a fixed-size buffer: 104 on macOS/BSD, 108 on Linux.
MAX_SOCKET_PATH = 100
HANDSHAKE_TIMEOUT_SECONDS = float(PUBLIC_LIMITS["handshake_timeout_seconds"])


class PeerIdentityError(ConnectionError):
    """The local peer's operating-system identity could not be trusted."""


def _libc_getpeereid(file_descriptor: int) -> tuple[int, int]:
    user = ctypes.c_uint()
    group = ctypes.c_uint()
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "getpeereid", None)
    if function is None or function(file_descriptor, ctypes.byref(user), ctypes.byref(group)) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return user.value, group.value


def peer_uid_from_socket(sock, *, platform: str | None = None, getpeereid=None) -> int:
    """Read peer UID from kernel-owned Unix-socket credentials."""
    platform = sys.platform if platform is None else platform
    if platform.startswith("linux"):
        # Linux has assigned 17 to SO_PEERCRED since the option was introduced.
        option = getattr(_socket, "SO_PEERCRED", 17)
        size = struct.calcsize("3i")
        raw = sock.getsockopt(_socket.SOL_SOCKET, option, size)
        if not isinstance(raw, bytes) or len(raw) != size:
            raise PeerIdentityError("SO_PEERCRED returned an invalid value")
        _pid, uid, _gid = struct.unpack("3i", raw)
        return uid
    if platform.startswith(("darwin", "freebsd", "openbsd", "netbsd")):
        method = getattr(sock, "getpeereid", None)
        if getpeereid is not None:
            uid, _gid = getpeereid(sock.fileno())
        elif callable(method):
            uid, _gid = method()
        else:
            uid, _gid = _libc_getpeereid(sock.fileno())
        return int(uid)
    raise PeerIdentityError(f"peer credentials are unsupported on {platform}")


def verify_peer_uid(writer, *, expected_uid: int | None = None, platform: str | None = None) -> int:
    """Fail closed unless the connected peer has the daemon's effective UID."""
    extra_info = getattr(writer, "get_extra_info", None)
    if not callable(extra_info):
        raise PeerIdentityError("connection does not expose a peer socket")
    sock = extra_info("socket")
    if sock is None:
        raise PeerIdentityError("connection does not expose a peer socket")
    try:
        peer_uid = peer_uid_from_socket(sock, platform=platform)
    except PeerIdentityError:
        raise
    except Exception as exc:
        raise PeerIdentityError(f"could not verify peer UID: {exc}") from exc
    expected = os.geteuid() if expected_uid is None else expected_uid
    if peer_uid != expected:
        raise PeerIdentityError(f"peer UID {peer_uid} does not match daemon UID {expected}")
    return peer_uid


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


async def _read_connection_message(reader, *, unclassified: bool, deadline: float) -> bytes:
    if not unclassified:
        return await protocol.read_message(reader)
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError
    return await asyncio.wait_for(protocol.read_message(reader), remaining)


def _callback_context(router):
    context = router.context
    if context is None or context.provider_connection_token is None:
        return None
    return context


async def _serve_callback_if_bound(daemon, router, reader, writer) -> bool:
    context = _callback_context(router)
    if context is None or context.channel.value != "callback":
        return False
    await daemon.terminal_service.serve_callback(context, reader, writer)
    return True


def _release_callback_if_bound(daemon, router) -> None:
    context = _callback_context(router) if router is not None else None
    if context is None or context.provider_id is None or context.provider_generation is None:
        return
    daemon.terminal_service.connections.disconnect(
        context.provider_id,
        context.provider_generation,
        token=context.provider_connection_token,
    )


async def handle_connection(
    daemon,
    reader,
    writer,
    *,
    private_methods=None,
    handshake_timeout: float | None = None,
) -> None:
    """Per-connection handler: read-dispatch-write until the client disconnects."""
    task = asyncio.current_task()
    router = None
    if task is not None:
        daemon._conns.add(task)
    try:
        try:
            verify_peer_uid(writer)
        except PeerIdentityError as exc:
            logger.warning("refusing unverifiable local peer: %s", exc)
            return
        from theater.daemon.frontend.router import ConnectionMode, ConnectionRouter

        router = ConnectionRouter(daemon, private_methods=private_methods)
        timeout = HANDSHAKE_TIMEOUT_SECONDS if handshake_timeout is None else handshake_timeout
        handshake_deadline = asyncio.get_running_loop().time() + max(timeout, 0.0)
        while True:
            try:
                line = await _read_connection_message(
                    reader,
                    unclassified=router.mode is ConnectionMode.UNCLASSIFIED,
                    deadline=handshake_deadline,
                )
            except TimeoutError:
                logger.debug("closing connection that did not classify before its deadline")
                break
            except protocol.MessageTooLarge as exc:
                # Answer with id 0 when the request was too large to read its real id.
                logger.warning("oversized request: %s", exc)
                writer.write(protocol.err(0, "too_large", str(exc)))
                await writer.drain()
                await protocol.drain_message(reader)
                continue
            if not line:
                break
            if len(line) > protocol.MAX_MESSAGE_BYTES:
                writer.write(
                    protocol.err(
                        0,
                        "too_large",
                        f"message exceeds {protocol.MAX_MESSAGE_BYTES} bytes",
                    )
                )
                await writer.drain()
                continue
            response = await router.dispatch(line)
            if not response:
                break
            writer.write(response)
            await writer.drain()
            if await _serve_callback_if_bound(daemon, router, reader, writer):
                break
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        _release_callback_if_bound(daemon, router)
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

    if not isinstance(msg, dict):
        return protocol.err(0, "bad_request", "request must be a JSON object")

    raw_id = msg.get("id", 0)
    req_id = raw_id if type(raw_id) is int else 0
    name = msg.get("method")
    if not isinstance(name, str):
        return protocol.err(req_id, "bad_request", "method must be a string")
    if "params" not in msg:
        params = {}
    else:
        params = msg["params"]
        if not isinstance(params, dict):
            return protocol.err(req_id, "bad_request", "params must be a JSON object")
    handler = methods.get(name)
    if handler is None:
        return protocol.err(req_id, "unknown_method", f"no method {name!r}")

    raw_meta = msg.get("_meta")
    parent_context = extract_trace_context(raw_meta) if isinstance(raw_meta, Mapping) else None

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
