"""Authenticated Unix listener for stock-frontend extensions."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import stat
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from theater.daemon.harness_runtime.backend import backend_artifacts_dir
from theater.daemon.harness_runtime.constants import FRONTEND_LINE_MAX_BYTES, FRONTEND_QUEUE_MAX
from theater.daemon.harness_runtime.frontend_requests import FrontendRequests
from theater.harness.contracts.runtime import (
    RuntimeConnectionClosed,
    RuntimeFrontendConnection,
    RuntimeNotification,
)

FrontendCallback = Callable[[RuntimeFrontendConnection], Awaitable[None]]

logger = logging.getLogger("theater.daemon.harness_runtime.frontend")


class FrontendProtocolError(ValueError):
    """A frontend sent an invalid local protocol frame."""


class UnixFrontendConnection(RuntimeFrontendConnection):
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        self._queue: asyncio.Queue[RuntimeNotification | None] = asyncio.Queue(FRONTEND_QUEUE_MAX)
        self._closed = False
        self._requests = FrontendRequests(self._send_frame)

    @property
    def closed(self) -> bool:
        return self._closed

    def publish(self, notification: RuntimeNotification) -> None:
        if self._closed:
            raise FrontendProtocolError("frontend connection is closed")
        if self._queue.full():
            raise FrontendProtocolError("frontend observation queue is full")
        self._queue.put_nowait(notification)

    async def request(
        self, method: str, params: Mapping[str, object], *, timeout: float
    ) -> Mapping[str, object]:
        return await self._requests.request(method, params, timeout=timeout)

    def receive_response(self, message: Mapping[str, object]) -> None:
        self._requests.receive(message)

    async def _send_frame(self, payload: bytes) -> None:
        if self._closed:
            raise RuntimeConnectionClosed("frontend connection is closed")
        try:
            self._writer.write(payload)
            await self._writer.drain()
        except ConnectionError as exc:
            raise RuntimeConnectionClosed("frontend connection was lost during write") from exc

    async def notifications(self) -> AsyncIterator[RuntimeNotification]:
        while True:
            if self._closed and self._queue.empty():
                return
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._requests.close()
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()


@dataclass(slots=True)
class _Listener:
    participant_id: str
    generation: int
    path: Path
    token: str
    server: asyncio.AbstractServer
    on_connect: FrontendCallback
    on_disconnect: FrontendCallback
    active: UnixFrontendConnection | None = None
    connection_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    handlers: set[asyncio.Task] = field(default_factory=set)
    closed: bool = False


class FrontendRuntimeHost:
    """Owns one frontend listener per participant, including handshake tasks."""

    def __init__(self) -> None:
        self._listeners: dict[str, _Listener] = {}
        self._lock = asyncio.Lock()

    async def start(
        self,
        *,
        participant_id: str,
        generation: int,
        endpoint: str,
        token: str,
        on_connect: FrontendCallback,
        on_disconnect: FrontendCallback,
    ) -> None:
        path = _endpoint_path(endpoint)
        async with self._lock:
            current = self._listeners.get(participant_id)
            if current is not None:
                if current.generation == generation and current.path == path:
                    return
                await self._close_listener(current)
            _validate_socket_path(participant_id, path)
            _remove_stale_socket(path)
            listener: _Listener | None = None

            async def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                assert listener is not None
                await self._handle(listener, reader, writer)

            server = await asyncio.start_unix_server(
                connected,
                path=str(path),
                limit=FRONTEND_LINE_MAX_BYTES + 1,
            )
            path.chmod(0o600)
            listener = _Listener(
                participant_id=participant_id,
                generation=generation,
                path=path,
                token=token,
                server=server,
                on_connect=on_connect,
                on_disconnect=on_disconnect,
            )
            self._listeners[participant_id] = listener

    async def close(self, participant_id: str) -> None:
        async with self._lock:
            listener = self._listeners.pop(participant_id, None)
            if listener is not None:
                await self._close_listener(listener)

    async def aclose(self) -> None:
        async with self._lock:
            listeners = tuple(self._listeners.values())
            self._listeners.clear()
            for listener in listeners:
                await self._close_listener(listener)

    async def _close_listener(self, listener: _Listener) -> None:
        listener.closed = True
        listener.server.close()
        handlers = tuple(listener.handlers)
        for task in handlers:
            task.cancel()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)
        # Python 3.12 waits for connected clients too; terminate handlers
        # before waiting for the listening server's transports to close.
        await listener.server.wait_closed()
        async with listener.connection_lock:
            active = listener.active
            listener.active = None
            if active is not None:
                await active.aclose()
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(5.0):
                        await listener.on_disconnect(active)
        _remove_stale_socket(listener.path)

    async def _handle(
        self,
        listener: _Listener,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        connection: UnixFrontendConnection | None = None
        reader_task: asyncio.Task | None = None
        handler = asyncio.current_task()
        assert handler is not None
        listener.handlers.add(handler)
        connected = False
        try:
            if listener.closed:
                return
            hello = await asyncio.wait_for(_read_message(reader), timeout=5.0)
            _validate_hello(hello, listener.token)
            connection = UnixFrontendConnection(writer)
            # Initial attachment can request a snapshot. The response reader
            # must already be running while the activation callback awaits it.
            reader_task = asyncio.create_task(_read_frames(reader, connection))
            connected = await _activate(listener, connection)
            if connected:
                await reader_task
        except (asyncio.IncompleteReadError, ConnectionError, FrontendProtocolError, ValueError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("frontend listener callback failed for %s", listener.participant_id)
        finally:
            if reader_task is not None:
                reader_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await reader_task
            if connection is not None:
                await connection.aclose()
                async with listener.connection_lock:
                    if listener.active is connection:
                        listener.active = None
                        if connected:
                            with contextlib.suppress(Exception):
                                async with asyncio.timeout(5.0):
                                    await listener.on_disconnect(connection)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            listener.handlers.discard(handler)


async def _activate(listener: _Listener, connection: UnixFrontendConnection) -> bool:
    async with listener.connection_lock:
        if listener.closed or connection.closed:
            return False
        previous = listener.active
        listener.active = connection
        if previous is not None:
            await previous.aclose()
            with contextlib.suppress(Exception):
                async with asyncio.timeout(5.0):
                    await listener.on_disconnect(previous)
        try:
            async with asyncio.timeout(10.0):
                await listener.on_connect(connection)
        except BaseException:
            if listener.active is connection:
                listener.active = None
            raise
        return True


async def _read_frames(reader: asyncio.StreamReader, connection: UnixFrontendConnection) -> None:
    try:
        while True:
            message = await _read_message(reader)
            if message.get("type") == "response":
                connection.receive_response(message)
            else:
                message_type = message.pop("type", None)
                connection.publish(_notification_for(message_type, message))
    finally:
        await connection.aclose()


async def _read_message(reader: asyncio.StreamReader) -> dict[str, object]:
    try:
        line = await reader.readline()
    except (ValueError, asyncio.LimitOverrunError) as exc:
        raise FrontendProtocolError("frontend frame exceeds the line limit") from exc
    if not line:
        raise asyncio.IncompleteReadError(line, None)
    if len(line) > FRONTEND_LINE_MAX_BYTES or not line.endswith(b"\n"):
        raise FrontendProtocolError("frontend frame exceeds the line limit")
    try:
        value = json.loads(line)
    except (TypeError, ValueError) as exc:
        raise FrontendProtocolError("frontend frame is not JSON") from exc
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise FrontendProtocolError("frontend frame must be a JSON object")
    return value


def _endpoint_path(endpoint: str) -> Path:
    parsed = urlparse(endpoint)
    if parsed.scheme != "unix" or parsed.netloc or not parsed.path:
        raise FrontendProtocolError("frontend endpoint must be an absolute unix URI")
    path = Path(parsed.path)
    if not path.is_absolute():
        raise FrontendProtocolError("frontend endpoint must be an absolute unix URI")
    return path


def _validate_hello(hello: Mapping[str, object], token: str) -> None:
    if hello.get("type") != "hello":
        raise FrontendProtocolError("first frontend message must be hello")
    supplied = hello.get("token")
    if not isinstance(supplied, str) or not hmac.compare_digest(supplied, token):
        raise FrontendProtocolError("frontend authentication failed")
    if hello.get("protocol") != "theater-frontend-v1":
        raise FrontendProtocolError("frontend protocol is incompatible")


def _notification_for(message_type: object, params: dict[str, object]) -> RuntimeNotification:
    if message_type not in {"event", "snapshot", "history"}:
        raise FrontendProtocolError("unsupported frontend message type")
    return RuntimeNotification(method=message_type, params=params)


def _validate_socket_path(participant_id: str, path: Path) -> None:
    expected = backend_artifacts_dir(participant_id).resolve(strict=False)
    if path.parent.resolve(strict=False) != expected or path.name != "frontend.sock":
        raise FrontendProtocolError(
            "frontend socket path is outside its participant runtime directory"
        )


def _remove_stale_socket(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        raise FrontendProtocolError("frontend socket path is occupied by a non-socket")
    path.unlink()


__all__ = ["FrontendProtocolError", "FrontendRuntimeHost", "UnixFrontendConnection"]
