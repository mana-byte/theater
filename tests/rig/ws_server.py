"""A minimal RFC 6455 WebSocket server over a unix socket, for engine tests.

Test rig only — production code must not import it. It implements just enough
of the server side to exercise the runtime engine's client against real
sockets: the HTTP Upgrade handshake, masked-frame acceptance (server frames
are unmasked), fragmented sends, ping/pong, close, and raw frame injection for
protocol-violation tests.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

from theater.daemon.harness_runtime.frames import (
    CLOSE_NORMAL,
    OP_CLOSE,
    OP_TEXT,
    FrameDecoder,
    FrameProtocolError,
    encode_frame,
)

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_FRAME_BYTES = 16 * 1024 * 1024

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


class WsTestServer:
    """One single-client WebSocket server bound to a unix socket path."""

    def __init__(self, path: Path, *, handler: Handler | None = None) -> None:
        self.path = path
        self.request_headers: dict[str, str] = {}
        self.request_line = ""
        self.frames: list[tuple[int, bytes]] = []
        self.handshake_complete = asyncio.Event()
        self._frame_ready = asyncio.Event()
        self._handler = handler or self._serve_client
        self._server: asyncio.AbstractServer | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._client_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_unix_server(self._client_connected, self.path)

    async def stop(self) -> None:
        """Close the listener and abort the live connection, deterministically.

        The 3.12 ``Server.wait_closed`` waits for handlers to finish, and a
        healthy client never finishes, so the connection task is cancelled
        and its writer closed — the client then observes a real EOF.
        """
        if self._server is not None:
            self._server.close()
            self._server = None
        task = self._client_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._writer is not None:
            self._writer.close()
            self._writer = None

    async def wait_handshake(self) -> None:
        await asyncio.wait_for(self.handshake_complete.wait(), 5.0)

    async def _client_connected(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._writer = writer
        self._client_task = asyncio.current_task()
        try:
            await self._handler(reader, writer)
        finally:
            writer.close()

    async def _serve_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        self.request_line = lines[0]
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            if separator:
                self.request_headers[name.strip().lower()] = value.strip()
        key = self.request_headers["sec-websocket-key"]
        accept = base64.b64encode(hashlib.sha1(f"{key}{_WS_GUID}".encode()).digest()).decode()
        writer.write(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n"
                "\r\n"
            ).encode("ascii")
        )
        await writer.drain()
        self.handshake_complete.set()
        decoder = FrameDecoder(expect_masked=True, max_frame_bytes=_MAX_FRAME_BYTES)
        while True:
            data = await reader.read(65536)
            if not data:
                return
            for frame in decoder.feed(data):
                if frame.opcode == OP_CLOSE:
                    self.frames.append((frame.opcode, frame.payload))
                    self._frame_ready.set()
                    self.send_frame(OP_CLOSE, frame.payload[:2])
                    return
                self.frames.append((frame.opcode, frame.payload))
                self._frame_ready.set()

    # ---- sending ----------------------------------------------------------

    def send_frame(self, opcode: int, payload: bytes, *, fin: bool = True) -> None:
        assert self._writer is not None, "test server has no live client connection"
        self._writer.write(encode_frame(opcode, payload, mask=False, fin=fin))
        # Drain happens on the event loop as soon as the test yields.

    def send_json(self, message: dict[str, object]) -> None:
        self.send_frame(OP_TEXT, json.dumps(message, separators=(",", ":")).encode("utf-8"))

    def send_json_fragments(self, message: dict[str, object], chunk_sizes: list[int]) -> None:
        """Send one JSON message split into explicit continuation frames."""
        payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
        assert self._writer is not None
        chunks: list[bytes] = []
        offset = 0
        for size in chunk_sizes:
            chunks.append(payload[offset : offset + size])
            offset += size
        if offset < len(payload):
            chunks.append(payload[offset:])
        if not chunks:
            chunks = [b""]
        for index, chunk in enumerate(chunks):
            opcode = OP_TEXT if index == 0 else 0x0
            fin = index == len(chunks) - 1
            self._writer.write(encode_frame(opcode, chunk, mask=False, fin=fin))
        # Drain happens on the event loop as soon as the test yields.

    def send_close(self, code: int = CLOSE_NORMAL) -> None:
        self.send_frame(OP_CLOSE, code.to_bytes(2, "big"))

    # ---- receiving --------------------------------------------------------

    async def next_frame(self, *, timeout: float = 5.0) -> tuple[int, bytes]:
        """The next frame the client sent, waiting deterministically."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if self.frames:
                return self.frames.pop(0)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("no client frame arrived within the test timeout")
            try:
                await asyncio.wait_for(self._frame_ready.wait(), remaining)
            except TimeoutError:
                raise TimeoutError("no client frame arrived within the test timeout") from None
            self._frame_ready.clear()

    async def drain_frames(self, count: int) -> None:
        """Wait until at least ``count`` frames arrived, deterministically."""
        deadline = asyncio.get_running_loop().time() + 5.0
        while len(self.frames) < count:
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"only {len(self.frames)} of {count} frames arrived")
            await asyncio.sleep(0)


__all__ = [
    "FrameProtocolError",
    "WsTestServer",
]
