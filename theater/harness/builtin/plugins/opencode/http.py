"""Bounded loopback HTTP/1.1 transport for the detached OpenCode server.

Errors carry method/path/status only — never credentials, request bodies, or payloads.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from theater.harness.contracts.runtime import RuntimeConnectionError

#: One control-plane request: connect, headers, and a bounded body, end to end.
REQUEST_DEADLINE_SECONDS = 15.0
#: Hard bound for one response body.
MAX_RESPONSE_BYTES = 1_048_576
#: One HTTP header line.
MAX_LINE_BYTES = 65_536
MAX_HEADER_LINES = 128
#: The Basic username the stock server authenticates (upstream serve.ts).
BASIC_USERNAME = "opencode"
#: The longest client-supplied session id used in a path segment.
MAX_SESSION_ID_CHARS = 128

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


class OpenCodeHttpError(RuntimeConnectionError):
    """One failed request, redacted by construction.

    ``written`` False is a safe REJECTED; True is an ambiguous UNKNOWN that must never be replayed.
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

    ``O_NOFOLLOW`` plus ``fstat`` on the same fd defeats symlink/chmod races; only owner 0600
    passes.
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


class OpenCodeClient:
    """Authenticated transport for one participant's server."""

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
