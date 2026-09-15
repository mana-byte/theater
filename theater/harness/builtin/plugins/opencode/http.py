"""Bounded loopback HTTP/1.1 and SSE client for the detached OpenCode server.

Loopback-only, Basic-authenticated, redirect-free, with explicit deadlines
and byte caps on every frame; errors carry method/path/status only — never
credentials, request bodies, or response payloads.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import stat
from collections.abc import AsyncGenerator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from theater.harness.contracts.runtime import RuntimeConnectionError

#: One control-plane request: connect, headers, and a bounded body, end to end.
REQUEST_DEADLINE_SECONDS = 15.0
#: Longest gap between SSE bytes before the subscription is considered lost.
SSE_IDLE_DEADLINE_SECONDS = 120.0
#: Hard bound for one response body and one assembled SSE event.
MAX_RESPONSE_BYTES = 1_048_576
MAX_SSE_EVENT_BYTES = 1_048_576
#: One HTTP header line and one SSE line.
MAX_LINE_BYTES = 65_536
MAX_HEADER_LINES = 128
#: Bounded client-side event buffer before the reader applies backpressure.
MAX_SSE_QUEUE = 256
#: The Basic username the stock server authenticates (upstream serve.ts).
BASIC_USERNAME = "opencode"
#: The longest client-supplied session id used in a path segment.
MAX_SESSION_ID_CHARS = 128

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


class OpenCodeHttpError(RuntimeConnectionError):
    """One failed request, redacted by construction.

    ``written`` tells the runtime whether every request byte was handed to
    the transport before the failure: False is a safe REJECTED, True is an
    ambiguous UNKNOWN that must never be replayed. The message never carries
    the Authorization header, the password, or any body text.
    """

    def __init__(
        self,
        method: str,
        path: str,
        reason: str,
        *,
        status: int | None = None,
        written: bool = False,
        session_id: str | None = None,
    ) -> None:
        self.method = method
        self.path = path
        self.status = status
        self.reason = reason
        self.written = written
        self.session_id = session_id
        delivery = "request bytes were delivered" if written else "request was not delivered"
        where = f" for session {session_id}" if session_id else ""
        code = f"HTTP {status} " if status is not None else ""
        super().__init__(f"{method} {path}{where} failed: {code}{reason} ({delivery})")


class OpenCodeStreamError(RuntimeConnectionError):
    """The SSE subscription ended; reconnect must reconcile, never replay."""


class _ClientSecret:
    """A runtime credential whose bytes never render."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def basic_header(self) -> str:
        raw = f"{BASIC_USERNAME}:{self._value}".encode()
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def __repr__(self) -> str:  # pragma: no cover - trivial guard
        return "<redacted runtime credential>"


def _validate_secret_file(info: os.stat_result, token_file: Path) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"runtime credential file {token_file.name} is not a regular file")
    if info.st_uid != os.geteuid():
        raise ValueError(f"runtime credential file {token_file.name} is not owned by us")
    if stat.S_IMODE(info.st_mode) & 0o177:
        raise ValueError(f"runtime credential file {token_file.name} must be mode 0600")


def _validate_secret_bytes(raw: bytes, token_file: Path) -> None:
    if not raw or len(raw) > 256 or b"\n" in raw:
        raise ValueError(f"runtime credential file {token_file.name} is not one bounded token")


def read_client_secret(token_file: Path) -> _ClientSecret:
    """Read the private runtime password, refusing unsafe files.

    The file is opened with ``O_NOFOLLOW`` and validated from the SAME fd's
    ``fstat`` — never a separate stat call — so a symlink or a mode change
    between check and read cannot redirect the password. Anything but an
    owner-held ``0600`` regular file with one bounded token is refused.
    """
    try:
        fd = os.open(token_file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ValueError(f"runtime credential file {token_file.name} is unreadable") from exc
    try:
        info = os.fstat(fd)
        _validate_secret_file(info, token_file)
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(257)
        _validate_secret_bytes(raw, token_file)
        return _ClientSecret(raw.decode("utf-8", errors="strict"))
    except ValueError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"runtime credential file {token_file.name} is unreadable") from exc
    finally:
        os.close(fd)


def validate_loopback_endpoint(endpoint: str) -> tuple[str, int]:
    """Accept only a bare loopback http origin; return (host, port)."""
    try:
        parsed = urlsplit(endpoint, allow_fragments=False)
    except ValueError as exc:
        raise ValueError(f"endpoint {endpoint!r} is not a parseable URL") from exc
    if parsed.scheme != "http":
        raise ValueError(f"endpoint {endpoint!r} is not http; loopback HTTP only")
    if parsed.username or parsed.password:
        raise ValueError(f"endpoint {endpoint!r} carries credentials in the URL")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError(f"endpoint {endpoint!r} must be a bare loopback origin")
    if parsed.hostname not in _LOOPBACK_HOSTS:
        raise ValueError(f"endpoint {endpoint!r} is not a literal loopback host")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"endpoint {endpoint!r} has no valid port") from exc
    if port is None or not 1 <= port <= 65_535:
        raise ValueError(f"endpoint {endpoint!r} has no usable port")
    return parsed.hostname, port


def _safe_session_id(session_id: str) -> str:
    """Accept one bounded id usable as a path segment, or raise."""
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError("native session id is not one bounded path-safe token")
    return session_id


@dataclass(frozen=True, slots=True)
class _Response:
    status: int
    reason: str
    content_type: str
    body: bytes | None


def _read_status_line(line: bytes) -> tuple[int, str]:
    if not line:
        raise ValueError("server closed before a status line")
    if len(line) > MAX_LINE_BYTES:
        raise ValueError("oversized status line")
    parts = line.decode("ascii", errors="strict").rstrip("\r\n").split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/1."):
        raise ValueError("unparseable status line")
    try:
        status = int(parts[1])
    except ValueError as exc:
        raise ValueError("status line carries no integer status") from exc
    return status, (parts[2] if len(parts) > 2 else "")


async def _read_header_block(reader: asyncio.StreamReader) -> dict[str, str]:
    headers: dict[str, str] = {}
    for _ in range(MAX_HEADER_LINES):
        line = await reader.readline()
        if line in (b"\r\n", b"\n"):
            return headers
        if len(line) > MAX_LINE_BYTES:
            raise ValueError("oversized header line")
        name, sep, value = line.decode("latin-1").rstrip("\r\n").partition(":")
        if not sep:
            raise ValueError("malformed header line")
        headers[name.strip().lower()] = value.strip()
    raise ValueError("too many header lines")


async def _read_chunked(reader: asyncio.StreamReader) -> bytes:
    total = bytearray()
    while True:
        size_line = await reader.readline()
        if len(size_line) > MAX_LINE_BYTES:
            raise ValueError("oversized chunk header")
        try:
            size = int(size_line.split(b";")[0].strip() or b"0", 16)
        except ValueError as exc:
            raise ValueError("malformed chunk header") from exc
        if size == 0:
            terminator = await reader.readline()
            if terminator not in (b"\r\n", b"\n", b""):
                raise ValueError("chunked body did not terminate cleanly")
            return bytes(total)
        if len(total) + size > MAX_RESPONSE_BYTES:
            raise ValueError(f"response body exceeds {MAX_RESPONSE_BYTES} bytes")
        total += await reader.readexactly(size)
        await reader.readexactly(2)  # chunk trailing CRLF


async def _read_body(reader: asyncio.StreamReader, headers: Mapping[str, str]) -> bytes:
    if headers.get("transfer-encoding", "").lower() == "chunked":
        return await _read_chunked(reader)
    if "content-length" in headers:
        try:
            length = int(headers["content-length"])
        except ValueError as exc:
            raise ValueError("invalid content-length") from exc
        if length > MAX_RESPONSE_BYTES:
            raise ValueError(f"response body exceeds {MAX_RESPONSE_BYTES} bytes")
        return await reader.readexactly(length) if length else b""
    body = await reader.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError(f"response body exceeds {MAX_RESPONSE_BYTES} bytes")
    return body


async def _close_writer(writer: asyncio.StreamWriter | None) -> None:
    if writer is None:
        return
    writer.close()
    with contextlib.suppress(ConnectionError, OSError):
        await writer.wait_closed()


def _put_terminal(queue: asyncio.Queue[object], error: OpenCodeStreamError | None) -> None:
    item: object = error
    while True:
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
        else:
            return


class OpenCodeClient:
    """The daemon's authenticated client for one participant's server."""

    def __init__(
        self,
        *,
        endpoint: str,
        token_file: Path,
        connect_deadline_seconds: float = 5.0,
    ) -> None:
        self._host, self._port = validate_loopback_endpoint(endpoint)
        self._authority = (
            f"[{self._host}]:{self._port}" if ":" in self._host else f"{self._host}:{self._port}"
        )
        self._endpoint = endpoint.rstrip("/")
        self._secret = read_client_secret(token_file)
        self._connect_deadline = connect_deadline_seconds

    # ---- request plumbing ------------------------------------------

    def _request_lines(self, method: str, path: str, *, accept: str | None) -> list[str]:
        lines = [
            f"{method} {path} HTTP/1.1",
            f"Host: {self._authority}",
            f"Authorization: {self._secret.basic_header()}",
            "Connection: close",
        ]
        if accept is not None:
            lines.append(f"Accept: {accept}")
        return lines

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port), self._connect_deadline
            )
        except (TimeoutError, OSError) as exc:
            raise OpenCodeHttpError("CONNECT", self._endpoint, f"connect failed: {exc!r}") from exc
        return reader, writer

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        session_id: str | None = None,
    ) -> _Response:
        lines = self._request_lines(method, path, accept=None)
        if body is not None:
            lines.append(f"Content-Length: {len(body)}")
            lines.append("Content-Type: application/json")
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")
        if body is not None:
            request += body
        writer = None
        written = False
        try:
            async with asyncio.timeout(REQUEST_DEADLINE_SECONDS):
                reader, writer = await self._connect()
                writer.write(request)
                written = True
                await writer.drain()
                response = await self._read_json_response(reader)
        except OpenCodeHttpError:
            raise
        except TimeoutError as exc:
            raise OpenCodeHttpError(
                method, path, "deadline exceeded", written=written, session_id=session_id
            ) from exc
        except (ConnectionError, OSError, asyncio.IncompleteReadError) as exc:
            raise OpenCodeHttpError(
                method, path, f"connection failed: {exc!r}", written=written, session_id=session_id
            ) from exc
        except ValueError as exc:
            raise OpenCodeHttpError(
                method, path, f"malformed response: {exc}", written=written, session_id=session_id
            ) from exc
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(ConnectionError, OSError):
                    await writer.wait_closed()
        if 300 <= response.status < 400:
            raise OpenCodeHttpError(
                method,
                path,
                "redirect refused; the server must stay on its loopback origin",
                status=response.status,
                written=written,
                session_id=session_id,
            )
        return response

    async def _read_json_response(self, reader: asyncio.StreamReader) -> _Response:
        status, reason = _read_status_line(await reader.readline())
        headers = await _read_header_block(reader)
        body = await _read_body(reader, headers)
        content_type = headers.get("content-type", "")
        if status < 300 and body and content_type.split(";")[0].strip() != "application/json":
            raise ValueError("response content type is not application/json")
        return _Response(status, reason, content_type, body)

    async def _json_request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, object] | None = None,
        session_id: str | None = None,
    ) -> object | None:
        payload = None if body is None else json.dumps(body).encode("utf-8")
        if payload is not None and len(payload) > MAX_RESPONSE_BYTES:
            raise OpenCodeHttpError(
                method,
                path,
                f"request body exceeds {MAX_RESPONSE_BYTES} bytes",
                written=False,
                session_id=session_id,
            )
        response = await self._request(method, path, body=payload, session_id=session_id)
        if not 200 <= response.status < 300:
            raise OpenCodeHttpError(
                method,
                path,
                f"unexpected response ({response.reason})",
                status=response.status,
                written=True,
                session_id=session_id,
            )
        if response.body in (None, b""):
            return None
        try:
            return json.loads(response.body.decode("utf-8", errors="strict"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise OpenCodeHttpError(
                method,
                path,
                f"response is not strict UTF-8 JSON: {exc}",
                status=response.status,
                written=True,
                session_id=session_id,
            ) from exc

    # ---- named server surfaces -------------------------------------

    async def health(self) -> str:
        """Return the server's version; any other answer raises."""
        result = await self._json_request("GET", "/global/health")
        if not isinstance(result, Mapping) or "version" not in result:
            raise OpenCodeHttpError(
                "GET", "/global/health", "health response carries no version", written=True
            )
        return str(result["version"])

    async def create_session(self, *, title: str | None = None) -> str:
        body: dict[str, object] = {}
        if title is not None:
            body["title"] = title
        result = await self._json_request("POST", "/session", body=body)
        if not isinstance(result, Mapping) or not isinstance(result.get("id"), str):
            raise OpenCodeHttpError(
                "POST", "/session", "session response carries no string id", written=True
            )
        return _safe_session_id(result["id"])

    async def read_session(self, session_id: str) -> Mapping[str, object]:
        session_id = _safe_session_id(session_id)
        result = await self._json_request("GET", f"/session/{session_id}", session_id=session_id)
        if not isinstance(result, Mapping):
            raise OpenCodeHttpError(
                "GET",
                f"/session/{session_id}",
                "session readback is not an object",
                written=True,
                session_id=session_id,
            )
        return result

    async def fork_session(self, session_id: str) -> str:
        """Fork one session; return the new session's exact id."""
        session_id = _safe_session_id(session_id)
        result = await self._json_request(
            "POST", f"/session/{session_id}/fork", body={}, session_id=session_id
        )
        if not isinstance(result, Mapping) or not isinstance(result.get("id"), str):
            raise OpenCodeHttpError(
                "POST",
                f"/session/{session_id}/fork",
                "fork response carries no string id",
                written=True,
                session_id=session_id,
            )
        return _safe_session_id(result["id"])

    async def list_messages(self, session_id: str) -> tuple[Mapping[str, object], ...]:
        """Read back the session's durable message list, oldest first."""
        session_id = _safe_session_id(session_id)
        result = await self._json_request(
            "GET", f"/session/{session_id}/message", session_id=session_id
        )
        if not isinstance(result, list):
            raise OpenCodeHttpError(
                "GET",
                f"/session/{session_id}/message",
                "message readback is not a list",
                written=True,
                session_id=session_id,
            )
        return tuple(item for item in result if isinstance(item, Mapping))

    async def session_status(self) -> Mapping[str, object]:
        result = await self._json_request("GET", "/session/status")
        if not isinstance(result, Mapping):
            raise OpenCodeHttpError(
                "GET", "/session/status", "status response is not an object", written=True
            )
        return result

    async def prompt_async(self, session_id: str, body: Mapping[str, object]) -> object | None:
        session_id = _safe_session_id(session_id)
        return await self._json_request(
            "POST", f"/session/{session_id}/prompt_async", body=body, session_id=session_id
        )

    async def abort(self, session_id: str) -> object | None:
        session_id = _safe_session_id(session_id)
        return await self._json_request(
            "POST", f"/session/{session_id}/abort", body={}, session_id=session_id
        )

    # ---- SSE ---------------------------------------------------------

    async def _open_event_stream(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        request = (
            "\r\n".join(self._request_lines("GET", "/event", accept="text/event-stream"))
            + "\r\n\r\n"
        ).encode("ascii")
        writer: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout(REQUEST_DEADLINE_SECONDS):
                reader, writer = await self._connect()
                writer.write(request)
                await writer.drain()
                status, _ = _read_status_line(await reader.readline())
                headers = await _read_header_block(reader)
        except asyncio.CancelledError:
            await _close_writer(writer)
            raise
        except (
            TimeoutError,
            ConnectionError,
            OSError,
            asyncio.IncompleteReadError,
            OpenCodeHttpError,
        ) as exc:
            await _close_writer(writer)
            raise OpenCodeStreamError(f"subscription failed: {exc!r}") from exc
        except ValueError as exc:
            await _close_writer(writer)
            raise OpenCodeStreamError(f"malformed subscription response: {exc}") from exc
        if status != 200:
            await _close_writer(writer)
            raise OpenCodeStreamError(f"subscription failed: HTTP {status}")
        if headers.get("content-type", "").split(";")[0].strip() != "text/event-stream":
            await _close_writer(writer)
            raise OpenCodeStreamError("subscription is not text/event-stream")
        assert writer is not None
        return reader, writer

    async def _consume_events(
        self, reader: asyncio.StreamReader, queue: asyncio.Queue[object]
    ) -> None:
        """Parse the stream into the queue with backpressure, never dropping.

        A blocking ``put`` applies backpressure to the network reader, so a
        slow consumer slows the stream instead of losing identity or terminal
        events. Any boundary violation ends the subscription loudly.
        """
        data_lines: list[bytes] = []
        terminal: OpenCodeStreamError | None = None
        try:
            while True:
                async with asyncio.timeout(SSE_IDLE_DEADLINE_SECONDS):
                    line = await reader.readline()
                _check_sse_line(line)
                stripped = line.rstrip(b"\r\n")
                if stripped == b"":
                    if data_lines:
                        await queue.put(_decode_event(b"\n".join(data_lines)))
                        data_lines = []
                    continue
                if stripped.startswith(b":"):
                    continue
                name, _, value = stripped.partition(b":")
                value = value[1:] if value.startswith(b" ") else value
                if name == b"data":
                    _check_sse_event_size(data_lines, value)
                    data_lines.append(value)
                # `event:` and `id:` fields are accepted but carry no
                # replayable identity in the qualified release.
        except OpenCodeStreamError as exc:
            terminal = exc
        except ValueError:
            terminal = OpenCodeStreamError("oversized SSE line")
        except (TimeoutError, ConnectionError, OSError) as exc:
            terminal = OpenCodeStreamError(f"event stream lost: {exc!r}")
        finally:
            _put_terminal(queue, terminal)

    async def events(
        self, *, on_open: Callable[[], None] | None = None
    ) -> AsyncGenerator[Mapping[str, object], None]:
        """Yield parsed server events from one bounded subscription.

        Raises ``OpenCodeStreamError`` on disconnect, deadline, or any limit:
        the caller owns reconnect and readback reconciliation, and nothing is
        dropped silently. Payload text never appears in the error.
        """
        reader, writer = await self._open_event_stream()
        if on_open is not None:
            on_open()
        queue: asyncio.Queue[object] = asyncio.Queue(MAX_SSE_QUEUE)
        consumer = asyncio.create_task(self._consume_events(reader, queue))
        try:
            while True:
                item = await queue.get()
                if isinstance(item, OpenCodeStreamError):
                    raise item
                if item is None:
                    raise OpenCodeStreamError("event stream ended")
                yield item  # type: ignore[misc]
        finally:
            consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await consumer
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()


def _check_sse_line(line: bytes) -> None:
    if not line:
        raise OpenCodeStreamError("server closed the event stream")
    if len(line) > MAX_LINE_BYTES:
        raise OpenCodeStreamError("oversized SSE line")


def _check_sse_event_size(data_lines: list[bytes], value: bytes) -> None:
    if sum(len(part) for part in data_lines) + len(value) > MAX_SSE_EVENT_BYTES:
        raise OpenCodeStreamError("oversized SSE event")


def _decode_event(payload: bytes) -> Mapping[str, object]:
    if len(payload) > MAX_SSE_EVENT_BYTES:
        raise OpenCodeStreamError("oversized SSE event")
    try:
        decoded = json.loads(payload.decode("utf-8", errors="strict"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise OpenCodeStreamError(f"malformed SSE event: {exc}") from exc
    if not isinstance(decoded, Mapping):
        raise OpenCodeStreamError("SSE event is not a JSON object")
    return decoded
