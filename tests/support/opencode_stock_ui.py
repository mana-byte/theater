"""A recording frontend connection and a scripted provider for the stock OpenCode TUI proof."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field

from theater.harness.builtin.plugins.opencode.live import OpenCodeTuiLiveSource
from theater.harness.builtin.plugins.opencode.runtime import OpenCodeFrontendRuntime
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.runtime import (
    RuntimeContext,
    RuntimeFrontendConnection,
    RuntimeIO,
    RuntimeNotification,
)
from theater.models import Participant


class StockIO(RuntimeIO):
    async def connect(self, endpoint: str, *, timeout: float):
        del timeout
        raise AssertionError(f"the stock gate never opens a backend connection: {endpoint}")


class RecordingConnection(RuntimeFrontendConnection):
    """Records notifications while the runtime stays the sole consumer."""

    def __init__(self, inner: RuntimeFrontendConnection, sink: list[RuntimeNotification]) -> None:
        self._inner = inner
        self._sink = sink

    @property
    def closed(self) -> bool:
        return self._inner.closed

    async def notifications(self) -> AsyncIterator[RuntimeNotification]:
        async for item in self._inner.notifications():
            self._sink.append(item)
            yield item

    async def request(self, method: str, params: Mapping[str, object], *, timeout: float):
        return await self._inner.request(method, params, timeout=timeout)

    async def aclose(self) -> None:
        await self._inner.aclose()


def assistant_parent_ids(probe: Probe) -> list[object]:
    """Every parentID the forwarded assistant message.updated events named."""
    parents: list[object] = []
    for item in probe.notifications:
        event = item.params.get("event")
        if not isinstance(event, Mapping) or event.get("type") != "message.updated":
            continue
        properties = event.get("properties")
        info = properties.get("info") if isinstance(properties, Mapping) else None
        if isinstance(info, Mapping) and info.get("role") == "assistant":
            parents.append(info.get("parentID"))
    return parents


@dataclass(slots=True)
class Probe:
    """Daemon-shaped wiring: one OpenCode runtime per plugin connection."""

    notifications: list[RuntimeNotification] = field(default_factory=list)
    connections: list[RuntimeFrontendConnection] = field(default_factory=list)
    disconnected: list[RuntimeFrontendConnection] = field(default_factory=list)
    trusted_session_id: str | None = None
    runtimes: list[OpenCodeFrontendRuntime] = field(default_factory=list)

    @property
    def live(self) -> OpenCodeTuiLiveSource:
        assert self.runtimes, "no plugin connection yet"
        return self.runtimes[-1].live_source()

    async def on_connect(self, connection: RuntimeFrontendConnection) -> None:
        self.connections.append(connection)
        runtime = OpenCodeFrontendRuntime(
            RuntimeContext(
                participant_id="open-1",
                cwd="/tmp",
                io=StockIO(),
                backend_generation=1,
                endpoint="unix:///tmp/oc-stock.sock",
                frontend=RecordingConnection(connection, self.notifications),
                trusted_session_id_provider=lambda: self.trusted_session_id,
            )
        )
        self.runtimes.append(runtime)
        runtime.live_source()

    async def on_disconnect(self, connection: RuntimeFrontendConnection) -> None:
        self.disconnected.append(connection)


@dataclass(frozen=True, slots=True)
class StockLaunch:
    participant: Participant
    endpoint: str
    plan: LaunchPlan
    token: str
    session_id: str


class OpenAICompatibleProvider:
    def __init__(self) -> None:
        self.request_received = asyncio.Event()
        self.response_completed = asyncio.Event()
        self._release = asyncio.Event()
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._handlers: set[asyncio.Task[None]] = set()
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.errors: list[str] = []

    @property
    def base_url(self) -> str:
        assert self._server is not None
        socket = next(iter(self._server.sockets or ()), None)
        assert socket is not None
        host, port = socket.getsockname()[:2]
        return f"http://{host}:{port}/v1"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, host="127.0.0.1", port=0)

    def release(self) -> None:
        self._release.set()

    async def aclose(self) -> None:
        self._release.set()
        server = self._server
        if server is not None:
            server.close()
        for writer in tuple(self._writers):
            writer.close()
        for task in tuple(self._handlers):
            if not task.done():
                task.cancel()
        if self._handlers:
            await asyncio.gather(*self._handlers, return_exceptions=True)
        if server is not None:
            await server.wait_closed()

    def detail(self) -> str:
        return f"requests={self.requests!r}, errors={self.errors!r}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
        self._writers.add(writer)
        try:
            request_line = (await reader.readline()).decode("latin-1").rstrip("\r\n")
            method, target, _version = request_line.split(" ", 2)
            headers = await http_headers(reader)
            length = int(headers.get("content-length", "0"))
            body = await reader.readexactly(length) if length else b""
            payload = json.loads(body) if body else {}
            if not isinstance(payload, dict):
                self.errors.append("OpenAI-compatible request body was not an object")
                return
            self.requests.append((target, payload))
            if method == "GET" and target == "/v1/models":
                await http_json(writer, {"object": "list", "data": [{"id": "test-model"}]})
                return
            if method != "POST" or target != "/v1/chat/completions":
                await http_json(
                    writer, {"error": {"message": "unsupported test endpoint"}}, status=404
                )
                return
            self.request_received.set()
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Cache-Control: no-cache\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            await self._release.wait()
            for chunk in chat_completion_chunks():
                writer.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                await writer.drain()
            writer.write(b"data: [DONE]\n\n")
            await writer.drain()
            self.response_completed.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            self._writers.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            if task is not None:
                self._handlers.discard(task)


async def http_headers(reader: asyncio.StreamReader) -> dict[str, str]:
    headers: dict[str, str] = {}
    while line := await reader.readline():
        if line in {b"\r\n", b"\n"}:
            return headers
        key, value = line.decode("latin-1").split(":", 1)
        headers[key.lower()] = value.strip()
    raise ConnectionError("OpenAI-compatible request ended before headers")


async def http_json(
    writer: asyncio.StreamWriter, payload: Mapping[str, object], *, status: int = 200
) -> None:
    body = json.dumps(payload).encode()
    header = (
        f"HTTP/1.1 {status} {'OK' if status == 200 else 'Not Found'}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    writer.write(header + body)
    await writer.drain()


def chat_completion_chunks() -> tuple[dict[str, object], ...]:
    common = {
        "id": "chatcmpl-stock-proof",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "test-model",
    }
    return (
        {
            **common,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "stock streamed answer"},
                    "finish_reason": None,
                }
            ],
        },
        {
            **common,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
    )
