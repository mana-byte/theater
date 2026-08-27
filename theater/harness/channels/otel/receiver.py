"""Loopback-only bounded HTTP receiver for native OTel exports."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from theater.constants.harness import (
    HARNESS_OTEL_HTTP_BACKLOG,
    HARNESS_OTEL_HTTP_MAX_CONCURRENT_REQUESTS,
    HARNESS_OTEL_HTTP_MAX_HEADER_BYTES,
    HARNESS_OTEL_HTTP_MAX_HEADERS,
    HARNESS_OTEL_HTTP_REQUEST_TIMEOUT_SECONDS,
    HARNESS_OTEL_MAX_PAYLOAD_BYTES,
)


class OtelHttpError(ValueError):
    """One native OTel HTTP request is malformed or disallowed."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        self.status = status
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class OtelHttpResponse:
    """The bounded OTLP response for one accepted export."""

    rejected_records: int = 0
    error_message: str | None = None

    def __post_init__(self) -> None:
        if type(self.rejected_records) is not int or self.rejected_records < 0:
            raise ValueError("native OTel rejected_records must be a non-negative integer")


type OtelHttpIngress = Callable[[str, Mapping[str, str], bytes], Awaitable[OtelHttpResponse]]

_HTTP_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


class NativeOtelReceiver:
    """Own one independently managed loopback HTTP listener."""

    def __init__(
        self,
        ingest: OtelHttpIngress,
        *,
        host: str = "127.0.0.1",
        max_concurrent: int | None = None,
        request_timeout: float | None = None,
        backlog: int | None = None,
    ) -> None:
        _validate_loopback(host)
        self._ingest = ingest
        self._host = host
        self._max_concurrent = (
            HARNESS_OTEL_HTTP_MAX_CONCURRENT_REQUESTS if max_concurrent is None else max_concurrent
        )
        self._request_timeout = (
            HARNESS_OTEL_HTTP_REQUEST_TIMEOUT_SECONDS
            if request_timeout is None
            else request_timeout
        )
        self._backlog = HARNESS_OTEL_HTTP_BACKLOG if backlog is None else backlog
        if type(self._max_concurrent) is not int or self._max_concurrent <= 0:
            raise ValueError("native OTel concurrent request limit must be a positive integer")
        if type(self._backlog) is not int or self._backlog <= 0:
            raise ValueError("native OTel backlog must be a positive integer")
        if not math.isfinite(self._request_timeout) or self._request_timeout <= 0:
            raise ValueError("native OTel request timeout must be finite and positive")
        self._server: asyncio.AbstractServer | None = None
        self._handlers: set[asyncio.Task[object]] = set()
        self._rejectors: set[asyncio.Task[object]] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._in_flight = 0
        self._closed = False

    @property
    def endpoint(self) -> str:
        """Return the loopback endpoint after the listener starts."""
        sockets = getattr(self._server, "sockets", ()) if self._server is not None else ()
        if not sockets:
            raise RuntimeError("native OTel receiver is not running")
        port = sockets[0].getsockname()[1]
        return f"http://{self._host}:{port}"

    @property
    def port(self) -> int:
        """Return the listener port after startup."""
        return int(self.endpoint.rsplit(":", 1)[1])

    async def start(self, *, preferred_port: int = 0) -> None:
        """Bind the requested loopback port without fallback."""
        if self._closed:
            raise RuntimeError("native OTel receiver is closed")
        if type(preferred_port) is not int or not 0 <= preferred_port <= 65_535:
            raise ValueError("native OTel receiver port must be between 0 and 65535")
        if self._server is not None:
            if preferred_port and preferred_port != self.port:
                raise RuntimeError("native OTel receiver is already bound to a different port")
            return
        self._server = await asyncio.start_server(
            self._accept,
            host=self._host,
            port=preferred_port,
            backlog=self._backlog,
            limit=HARNESS_OTEL_HTTP_MAX_HEADER_BYTES,
        )

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._closed:
            self._reject(reader, writer, 503, "native OTel receiver is unavailable")
            return
        if self._in_flight >= self._max_concurrent:
            self._reject(reader, writer, 429, "native OTel receiver is overloaded")
            return
        self._in_flight += 1
        self._writers.add(writer)
        task = asyncio.create_task(self._handle(reader, writer))
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    def _reject(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        status: int,
        detail: str,
    ) -> None:
        if len(self._rejectors) >= self._max_concurrent:
            writer.close()
            return
        self._writers.add(writer)
        task = asyncio.create_task(self._send_rejection(reader, writer, status, detail))
        self._rejectors.add(task)
        task.add_done_callback(self._rejectors.discard)

    async def _send_rejection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        status: int,
        detail: str,
    ) -> None:
        content_type = ["application/json"]
        try:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(self._request_timeout):
                    await _read_request(reader, content_type)
            await _reply(writer, status, content_type[0], detail)
        finally:
            self._writers.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        content_type = ["application/json"]
        try:
            async with asyncio.timeout(self._request_timeout):
                _validate_peer(writer)
                path, headers, body = await _read_request(reader, content_type)
                response = await self._ingest(path, headers, body)
        except TimeoutError:
            await _reply(writer, 408, content_type[0], "native OTel request timed out")
        except asyncio.CancelledError:
            raise
        except OtelHttpError as exc:
            await _reply(writer, exc.status, content_type[0], str(exc))
        except Exception:
            await _reply(writer, 400, content_type[0], "native OTel export was rejected")
        else:
            await _reply(writer, 200, content_type[0], response)
        finally:
            self._in_flight -= 1
            self._writers.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def aclose(self) -> None:
        """Close the listener and all bounded active requests."""
        if self._closed:
            return
        self._closed = True
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for writer in tuple(self._writers):
            writer.close()
        tasks = (*self._handlers, *self._rejectors)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._handlers.clear()
        self._rejectors.clear()
        self._writers.clear()


def _validate_loopback(host: str) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("native OTel receiver bind host must be a loopback address") from exc
    if address.version != 4 or not address.is_loopback:
        raise ValueError("native OTel receiver bind host must be an IPv4 loopback address")


def _validate_peer(writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    if not isinstance(peer, tuple) or not peer:
        raise OtelHttpError("native OTel sender must use loopback")
    try:
        address = ipaddress.ip_address(peer[0])
    except ValueError as exc:
        raise OtelHttpError("native OTel sender must use loopback") from exc
    if not address.is_loopback:
        raise OtelHttpError("native OTel sender must use loopback")


async def _read_request(  # noqa: PLR0912
    reader: asyncio.StreamReader,
    content_type: list[str],
) -> tuple[str, dict[str, str], bytes]:
    request = await _line(reader, "request line")
    header_bytes = len(request) + 2
    parts = request.split(" ")
    if len(parts) != 3 or parts[2] != "HTTP/1.1":
        raise OtelHttpError("native OTel request must be HTTP/1.1 POST")
    method = parts[0]
    if not _HTTP_TOKEN.fullmatch(method):
        raise OtelHttpError("native OTel HTTP method is malformed")
    if method != "POST":
        raise OtelHttpError("native OTel HTTP method is not enabled", status=405)
    path = parts[1]
    headers: dict[str, str] = {}
    for index in range(HARNESS_OTEL_HTTP_MAX_HEADERS + 1):
        line = await _line(reader, "header")
        header_bytes += len(line) + 2
        if header_bytes > HARNESS_OTEL_HTTP_MAX_HEADER_BYTES:
            raise OtelHttpError("native OTel headers exceed the maximum size")
        if not line:
            break
        if index == HARNESS_OTEL_HTTP_MAX_HEADERS:
            raise OtelHttpError("native OTel request has too many headers")
        if ":" not in line:
            raise OtelHttpError("native OTel header is malformed")
        key, value = line.split(":", 1)
        if not _HTTP_TOKEN.fullmatch(key):
            raise OtelHttpError("native OTel header is malformed")
        key = key.lower()
        if not key or key in headers:
            raise OtelHttpError("native OTel headers must be unique")
        headers[key] = value.strip()
    if headers.get("transfer-encoding") is not None:
        raise OtelHttpError("native OTel transfer encoding is unsupported")
    declared_type = headers.get("content-type")
    if declared_type not in {"application/json", "application/x-protobuf"}:
        raise OtelHttpError("native OTel content type is not enabled", status=415)
    content_type[0] = declared_type
    raw_length = headers.get("content-length")
    if raw_length is None or not raw_length.isascii() or not raw_length.isdecimal():
        raise OtelHttpError("native OTel content length is invalid")
    length = int(raw_length)
    if length > HARNESS_OTEL_MAX_PAYLOAD_BYTES:
        raise OtelHttpError("native OTel body exceeds the maximum size", status=413)
    try:
        body = await reader.readexactly(length)
    except asyncio.IncompleteReadError as exc:
        raise OtelHttpError("native OTel body is truncated") from exc
    return path, headers, body


async def _line(reader: asyncio.StreamReader, label: str) -> str:
    try:
        value = await reader.readline()
    except ValueError as exc:
        raise OtelHttpError(f"native OTel {label} exceeds the maximum size") from exc
    if not value.endswith(b"\r\n"):
        raise OtelHttpError(f"native OTel {label} is malformed")
    try:
        return value[:-2].decode("ascii")
    except UnicodeDecodeError as exc:
        raise OtelHttpError(f"native OTel {label} must be ASCII") from exc


async def _reply(
    writer: asyncio.StreamWriter,
    status: int,
    content_type: str,
    response: OtelHttpResponse | str,
) -> None:
    writer.write(_response_bytes(status, content_type, response))
    with contextlib.suppress(ConnectionError):
        await writer.drain()


def _response_bytes(
    status: int,
    content_type: str,
    response: OtelHttpResponse | str,
) -> bytes:
    reason = {
        200: "OK",
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        405: "Method Not Allowed",
        408: "Request Timeout",
        413: "Content Too Large",
        415: "Unsupported Media Type",
        429: "Too Many Requests",
        503: "Service Unavailable",
    }.get(status, "Bad Request")
    if status == 200:
        result = response if isinstance(response, OtelHttpResponse) else OtelHttpResponse()
        if content_type == "application/x-protobuf":
            payload = _protobuf_response(result)
            response_type = "application/x-protobuf"
        else:
            payload = _json_response(result)
            response_type = "application/json"
    else:
        detail = response if isinstance(response, str) else "native OTel export was rejected"
        if content_type == "application/x-protobuf":
            payload = _protobuf_status(status, detail)
            response_type = "application/x-protobuf"
        else:
            payload = json.dumps({"code": _status_code(status), "message": detail}).encode("utf-8")
            response_type = "application/json"
    return (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Length: {len(payload)}\r\n"
        f"Content-Type: {response_type}\r\n"
        "Connection: close\r\n\r\n".encode("ascii")
        + payload
    )


def _json_response(response: OtelHttpResponse) -> bytes:
    if response.rejected_records == 0:
        return b"{}"
    return json.dumps(
        {
            "partialSuccess": {
                "rejectedLogRecords": str(response.rejected_records),
                "errorMessage": response.error_message or "native OTel records were dropped",
            }
        }
    ).encode("utf-8")


def _protobuf_response(response: OtelHttpResponse) -> bytes:
    if response.rejected_records == 0:
        return b""
    message = (response.error_message or "native OTel records were dropped").encode("utf-8")
    partial = b"\x08" + _varint(response.rejected_records) + _protobuf_text(2, message)
    return _protobuf_text(1, partial)


def _protobuf_status(status: int, detail: str) -> bytes:
    return b"\x08" + _varint(_status_code(status)) + _protobuf_text(2, detail.encode("utf-8"))


def _status_code(status: int) -> int:
    return {
        400: 3,
        401: 16,
        403: 7,
        404: 5,
        405: 12,
        408: 4,
        413: 8,
        415: 3,
        429: 8,
        503: 14,
    }.get(status, 2)


def _protobuf_text(field: int, value: bytes) -> bytes:
    return _varint((field << 3) | 2) + _varint(len(value)) + value


def _varint(value: int) -> bytes:
    encoded = bytearray()
    while value > 0x7F:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


__all__ = ["NativeOtelReceiver", "OtelHttpError", "OtelHttpResponse"]
