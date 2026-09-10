"""Minimal WebSocket-over-Unix JSON-RPC client for the Codex app-server.

This module exists to answer one Wave 0 feasibility question against the
unmodified stock release: can a Theater-like control client watch a promptless
`codex --remote unix://SOCKET` bootstrap, learn the exact UI-created thread id
from native evidence, and then drive exactly one turn on that thread?

It is deliberately dependency-free (stdlib only): the app-server's Unix
listener speaks WebSocket frames after an HTTP Upgrade handshake
(codex-rs `app-server-transport/src/transport/unix_socket.rs`), and Theater's
locked dependency set has no WebSocket library. The framing below is the
minimum RFC 6455 surface the app-server needs: masked client text frames,
unmasked server text frames, ping/pong, and close.

Wire facts this client relies on (verified in codex-rs app-server-protocol
`src/rpc.rs`): requests are `{"method", "id", "params"}`, responses are
`{"id", "result"}`, errors are `{"id", "error": {"code", "message"}}`,
notifications are `{"method", "params"?}`, and a reply to a server request is
`{"method", "id", "response"}` — no `jsonrpc` field anywhere.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

DEFAULT_WAIT_TIMEOUT = 30.0


class ProtocolError(RuntimeError):
    """The app-server sent something this client cannot interpret."""


class RequestError(RuntimeError):
    """The app-server answered a request with a JSON-RPC error."""


@dataclass(frozen=True)
class Frame:
    """One decoded WebSocket frame."""

    fin: bool
    opcode: int
    payload: bytes


def build_upgrade_request(path: str, key: str) -> bytes:
    """The HTTP Upgrade bytes that open the app-server's Unix-socket WebSocket.

    No `Origin` header on purpose: the TCP WebSocket listener rejects requests
    that carry one, and the Unix listener is the transport under test.
    """
    return (
        f"GET {path} HTTP/1.1\r\n"
        "Host: codex-app-server\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    ).encode("ascii")


def expected_accept_header(key: str) -> str:
    digest = hashlib.sha1((key + _WEBSOCKET_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def encode_client_text_frame(payload: bytes, mask: bytes | None = None) -> bytes:
    """Encode one masked text frame (RFC 6455 5.3: client frames must mask)."""
    if mask is None:
        mask = secrets.token_bytes(4)
    if len(mask) != 4:
        raise ValueError("client frame mask must be exactly four bytes")
    header = bytes([0x80 | OP_TEXT])  # FIN + text
    length = len(payload)
    if length < 126:
        header += bytes([0x80 | length])
    elif length < 1 << 16:
        header += bytes([0x80 | 126]) + length.to_bytes(2, "big")
    else:
        header += bytes([0x80 | 127]) + length.to_bytes(8, "big")
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return header + mask + masked


def decode_server_frame(buffer: bytes) -> Frame | None:
    """Decode one server frame from `buffer`, or None when it is incomplete.

    Server frames are never masked (RFC 6455 5.1), so the payload needs no
    unmasking — only length parsing for the 7/16/64-bit forms.
    """
    if len(buffer) < 2:
        return None
    first, second = buffer[0], buffer[1]
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    if masked:
        raise ProtocolError("server frames must not be masked")
    length = second & 0x7F
    offset = 2
    if length == 126:
        if len(buffer) < 4:
            return None
        length = int.from_bytes(buffer[2:4], "big")
        offset = 4
    elif length == 127:
        if len(buffer) < 10:
            return None
        length = int.from_bytes(buffer[2:10], "big")
        offset = 10
    end = offset + length
    if len(buffer) < end:
        return None
    payload = bytes(buffer[offset:end])
    return Frame(fin=fin, opcode=opcode, payload=payload)


def _frame_consumed(buffer: bytes) -> int | None:
    """Bytes consumed by the next complete frame in `buffer`, or None."""
    if len(buffer) < 2:
        return None
    length = buffer[1] & 0x7F
    offset = 2
    if length == 126:
        if len(buffer) < 4:
            return None
        offset, length = 4, int.from_bytes(buffer[2:4], "big")
    elif length == 127:
        if len(buffer) < 10:
            return None
        offset, length = 10, int.from_bytes(buffer[2:10], "big")
    end = offset + length
    return end if len(buffer) >= end else None


@dataclass
class Received:
    """One classified incoming JSON-RPC message."""

    kind: str  # "notification" | "server_request" | "response" | "error"
    method: str | None
    id: Any
    payload: dict[str, Any]


@dataclass
class ClientSentinel:
    """Assertable facts about what this client did and did not send."""

    requests: list[dict[str, Any]] = field(default_factory=list)
    responses_to_server_requests: list[dict[str, Any]] = field(default_factory=list)
    notifications: list[dict[str, Any]] = field(default_factory=list)

    def count_outgoing_requests(self, method: str) -> int:
        return sum(1 for message in self.requests if message.get("method") == method)


class AppServerClient:
    """One WebSocket connection to a `codex app-server --listen unix://` socket."""

    def __init__(self, socket_path: str, name: str = "theater-probe") -> None:
        self.socket_path = socket_path
        self.name = name
        self.sent = ClientSentinel()
        self.received: list[Received] = []
        self.closed = False
        self.close_received = False
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._next_request_id = 1
        self._pending: dict[Any, asyncio.Future[dict[str, Any]]] = {}
        self._frames: asyncio.Queue[Received] = asyncio.Queue()

    # -- connection lifecycle -------------------------------------------------

    async def connect(self) -> None:
        reader, writer = await asyncio.open_unix_connection(self.socket_path)
        self._reader, self._writer = reader, writer
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        writer.write(build_upgrade_request("/", key))
        await writer.drain()
        headers = await reader.readuntil(b"\r\n\r\n")
        header_text = headers.decode("latin-1")
        if " 101 " not in header_text.splitlines()[0]:
            raise ProtocolError(f"upgrade rejected: {header_text.splitlines()[0]}")
        accept = next(
            (
                line.split(":", 1)[1].strip()
                for line in header_text.splitlines()
                if line.lower().startswith("sec-websocket-accept:")
            ),
            None,
        )
        if accept != expected_accept_header(key):
            raise ProtocolError("upgrade response Sec-WebSocket-Accept mismatch")
        self._receive_task = asyncio.create_task(self._receive_loop())

    async def _receive_loop(self) -> None:
        assert self._reader is not None
        buffer = bytearray()
        while True:
            try:
                chunk = await self._reader.read(4096)
            except (asyncio.CancelledError, OSError):
                return
            if not chunk:
                self.closed = True
                self._fail_pending("connection closed by app-server")
                return
            buffer += chunk
            while True:
                consumed = _frame_consumed(bytes(buffer))
                if consumed is None:
                    break
                raw = bytes(buffer[:consumed])
                del buffer[:consumed]
                frame = decode_server_frame(raw)
                assert frame is not None
                if frame.opcode == OP_PING:
                    await self._send_pong(frame.payload)
                    continue
                if frame.opcode == OP_CLOSE:
                    self.close_received = True
                    continue
                if frame.opcode in (OP_TEXT, OP_CONTINUATION):
                    self._handle_bytes(frame.payload, final=frame.fin)
                # Binary frames are not part of the app-server protocol; ignore.

    def _handle_bytes(self, payload: bytes, *, final: bool) -> None:
        if not final:
            # The app-server never fragments, but tolerate continuation data.
            return
        message = json.loads(payload.decode("utf-8"))
        if "method" in message:
            if "id" in message:
                received = Received(
                    kind="server_request",
                    method=message["method"],
                    id=message["id"],
                    payload=message.get("params") or {},
                )
                self.received.append(received)
                self._frames.put_nowait(received)
            else:
                received = Received(
                    kind="notification",
                    method=message["method"],
                    id=None,
                    payload=message.get("params") or {},
                )
                self.received.append(received)
                self._frames.put_nowait(received)
        elif "result" in message:
            future = self._pending.pop(message["id"], None)
            if future is not None and not future.done():
                future.set_result(message["result"])
            self.received.append(
                Received(kind="response", method=None, id=message["id"], payload=message["result"])
            )
        elif "error" in message:
            future = self._pending.pop(message["id"], None)
            error = message["error"]
            if future is not None and not future.done():
                future.set_exception(RequestError(error.get("message", "unknown error")))
            self.received.append(
                Received(kind="error", method=None, id=message["id"], payload=error)
            )

    def _fail_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ProtocolError(reason))
        self._pending.clear()

    # -- outgoing ----------------------------------------------------------

    async def _send_json(self, message: dict[str, Any]) -> None:
        if self._writer is None or self.closed:
            raise ProtocolError("client is not connected")
        self._writer.write(encode_client_text_frame(json.dumps(message).encode("utf-8")))
        await self._writer.drain()

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self._next_request_id
        self._next_request_id += 1
        message: dict[str, Any] = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = params
        self.sent.requests.append(message)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send_json(message)
        return await future

    async def notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        self.sent.notifications.append(message)
        await self._send_json(message)

    async def respond_to_server_request(
        self, request_id: Any, method: str, payload: dict[str, Any]
    ) -> None:
        message = {"method": method, "id": request_id, "response": payload}
        self.sent.responses_to_server_requests.append(message)
        await self._send_json(message)

    async def _send_pong(self, payload: bytes) -> None:
        if self._writer is None:
            return
        mask = secrets.token_bytes(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        header = bytes([0x80 | OP_PONG, 0x80 | len(payload)])
        self._writer.write(header + mask + masked)
        await self._writer.drain()

    async def initialize(self, client_name: str = "theater_probe", version: str = "0.0.0") -> Any:
        """Complete the initialize/initialized handshake, like the native TUI."""
        result = await self.request(
            "initialize",
            {"clientInfo": {"name": client_name, "title": client_name, "version": version}},
        )
        await self.notification("initialized")
        return result

    # -- waits ---------------------------------------------------------------

    async def wait_for(
        self,
        kind: str,
        *,
        method: str | None = None,
        predicate: Callable[[Received], bool] | None = None,
        timeout: float = DEFAULT_WAIT_TIMEOUT,
    ) -> Received:
        """Wait for an incoming message that matches, by event, never by sleep."""
        deadline = asyncio.get_running_loop().time() + timeout
        for received in self.received:
            if self._matches(received, kind, method, predicate):
                return received
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(
                    f"{self.name}: no {kind} {method or ''} within {timeout}s; "
                    f"saw {[(r.kind, r.method) for r in self.received]}"
                )
            try:
                received = await asyncio.wait_for(self._frames.get(), remaining)
            except TimeoutError:
                raise TimeoutError(
                    f"{self.name}: no {kind} {method or ''} within {timeout}s; "
                    f"saw {[(r.kind, r.method) for r in self.received]}"
                ) from None
            if self._matches(received, kind, method, predicate):
                return received

    @staticmethod
    def _matches(
        received: Received,
        kind: str,
        method: str | None,
        predicate: Callable[[Received], bool] | None,
    ) -> bool:
        if received.kind != kind:
            return False
        if method is not None and received.method != method:
            return False
        return predicate is None or predicate(received)

    # -- teardown -------------------------------------------------------------

    async def close_abrupt(self) -> None:
        """Drop the connection without a close handshake, like a killed daemon."""
        if self._writer is not None:
            self._writer.transport.abort()
        self.closed = True
        self._fail_pending("connection closed abruptly")
        if getattr(self, "_receive_task", None) is not None:
            self._receive_task.cancel()

    async def aclose(self) -> None:
        """Polite close: send a close frame, then tear the transport down."""
        if self._writer is not None and not self.closed:
            mask = secrets.token_bytes(4)
            self._writer.write(bytes([0x80 | OP_CLOSE, 0x80 | 2]) + mask + b"\x03\xe8")
            with contextlib.suppress(OSError):
                await self._writer.drain()
        await self.close_abrupt()


def socket_connectable(path: str) -> bool:
    """True when a Unix socket at `path` accepts a connection right now."""
    if not Path(path).exists():
        return False
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _blocking_connectable(path)
    return True


def _blocking_connectable(path: str) -> bool:
    import socket

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.connect(path)
    except OSError:
        return False
    return True


async def wait_until(
    predicate: Callable[[], Any], *, timeout: float = 30.0, interval: float = 0.02
) -> Any:
    """Poll `predicate` until it returns truthy. For process-level facts only.

    Protocol readiness in this harness is always proven by events
    (`AppServerClient.wait_for`); this helper exists only for external facts
    that have no notification stream, such as the socket file appearing.
    """
    import time

    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError(f"condition not reached within {timeout}s")
        await asyncio.sleep(interval)
