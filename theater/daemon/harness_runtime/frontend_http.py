"""Bounded HTTP exchange for native extensions that cannot open a byte stream."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from theater.daemon.harness_runtime.constants import FRONTEND_LINE_MAX_BYTES, FRONTEND_QUEUE_MAX
from theater.daemon.harness_runtime.frontend_peer import FrontendPeer
from theater.daemon.harness_runtime.frontend_requests import json_default
from theater.harness.contracts.runtime import RuntimeConnectionClosed

HTTP_BODY_MAX_BYTES = 4 * FRONTEND_LINE_MAX_BYTES
HTTP_POLL_SECONDS = 0.25
HTTP_LEASE_SECONDS = 10.0


class HttpFrontendConnection(FrontendPeer):
    """One logical peer across short-lived HTTP connections; commands are never replayed."""

    def __init__(self, generation: int) -> None:
        super().__init__(include_deadline=True)
        self.generation = generation
        self._outbound: asyncio.Queue[Mapping[str, object] | None] = asyncio.Queue(
            FRONTEND_QUEUE_MAX
        )
        self._last_poll = time.monotonic()
        self._polled = asyncio.Event()

    def touch(self) -> None:
        self._last_poll = time.monotonic()
        self._polled.set()

    async def _send_frame(self, frame: Mapping[str, object], _payload: bytes) -> None:
        # FrontendRequests already serialized this frame, so it is JSON-safe as-is.
        if self.closed:
            raise RuntimeConnectionClosed("HTTP frontend connection is closed")
        try:
            self._outbound.put_nowait(frame)
        except asyncio.QueueFull as exc:
            raise RuntimeConnectionClosed("HTTP frontend outbound queue is full") from exc

    async def next_request(self) -> Mapping[str, object] | None:
        """Discard timed-out requests before a client could execute them late."""
        async with asyncio.timeout(HTTP_POLL_SECONDS):
            while not self.closed:
                request = await self._outbound.get()
                if request is None:
                    return None
                request_id = request.get("id")
                if isinstance(request_id, str) and self._requests.pending(request_id):
                    return request
        return None

    async def wait_for_disconnect(self) -> None:
        while not self.closed:
            self._polled.clear()
            remaining = HTTP_LEASE_SECONDS - (time.monotonic() - self._last_poll)
            if remaining <= 0:
                return
            try:
                await asyncio.wait_for(self._polled.wait(), remaining)
            except TimeoutError:
                return

    async def aclose(self) -> None:
        await super().aclose()
        self._polled.set()
        if not self._outbound.full():
            self._outbound.put_nowait(None)


@dataclass(frozen=True, slots=True)
class HttpExchange:
    """One bounded HTTP exchange body: the hello envelope plus its message batch."""

    hello: dict[str, object]
    generation: int
    messages: list[dict[str, object]]


async def read_exchange(reader: asyncio.StreamReader, first: bytes) -> HttpExchange:
    if first != b"POST /exchange HTTP/1.1\r\n":
        raise ValueError("unsupported frontend HTTP request")
    headers: dict[str, str] = {}
    size = len(first)
    for _ in range(32):
        line = await reader.readline()
        size += len(line)
        if size > 8192 or not line.endswith(b"\r\n"):
            raise ValueError("invalid frontend HTTP headers")
        if line == b"\r\n":
            break
        name, separator, value = line.decode("ascii").partition(":")
        name = name.lower()
        if not separator or not name or name in headers:
            raise ValueError("duplicate or malformed frontend HTTP header")
        headers[name] = value.strip()
    else:
        raise ValueError("too many frontend HTTP headers")
    length = headers.get("content-length", "")
    if (
        "transfer-encoding" in headers
        or headers.get("content-type", "").split(";", 1)[0] != "application/json"
        or not length.isascii()
        or not length.isdecimal()
        or not 0 < int(length) <= HTTP_BODY_MAX_BYTES
    ):
        raise ValueError("invalid frontend HTTP body length or encoding")
    packet = json.loads(await reader.readexactly(int(length)))
    if not isinstance(packet, dict):
        raise TypeError("frontend HTTP body must be an object")
    raw_messages = packet.get("messages", [])
    generation = packet.get("generation")
    if (
        type(generation) is not int
        or not 0 < generation < 2**53
        or not isinstance(raw_messages, list)
        or len(raw_messages) > 64
        or any(not isinstance(message, dict) for message in raw_messages)
    ):
        raise ValueError("invalid frontend generation or messages")
    return HttpExchange(
        hello=packet,
        generation=generation,
        messages=cast(list[dict[str, object]], raw_messages),
    )


async def reply(writer: asyncio.StreamWriter, status: int, value: Mapping[str, object]) -> None:
    body = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=json_default
    ).encode()
    writer.write(
        f"HTTP/1.1 {status} {'OK' if status == 200 else 'Refused'}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n".encode("ascii")
        + body
    )
    await writer.drain()
