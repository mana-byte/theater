"""Authenticated HTTP transport shared by OpenCode's ordinary runtime test fakes."""

from __future__ import annotations

import asyncio
import base64
from typing import Any

from theater.harness.builtin.plugins.opencode.http import (
    BASIC_USERNAME,
)

PASSWORD = "test-password-1234"


class ServerFake:
    """Record requests; subclasses implement the supported API routes."""

    def __init__(self, *, password: str = PASSWORD) -> None:
        self._password = password
        self._server: asyncio.AbstractServer | None = None
        self.port = 0
        self.requests: list[dict[str, Any]] = []
        self.sessions: dict[str, dict[str, object]] = {}
        self._next = 0

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        assert self._server.sockets
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()

    def _mint_session(self) -> str:
        self._next += 1
        session_id = f"ses_fake_{self._next}"
        self.sessions[session_id] = {"id": session_id}
        return session_id

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            parts = request_line.decode("latin-1").rstrip("\r\n").split(" ")
            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if not line:
                    return
                if line in (b"\r\n", b"\n"):
                    break
                name, _, value = line.decode("latin-1").rstrip("\r\n").partition(":")
                headers[name.strip().lower()] = value.strip()
            body = b""
            if "content-length" in headers:
                body = await reader.readexactly(int(headers["content-length"]))
            record = {
                "method": parts[0],
                "path": parts[1] if len(parts) > 1 else "",
                "headers": headers,
                "body": body,
            }
            self.requests.append(record)
            await self._route(record, writer, body)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    def _authorized(self, record: dict[str, Any]) -> bool:
        expected = base64.b64encode(f"{BASIC_USERNAME}:{self._password}".encode()).decode("ascii")
        return record["headers"].get("authorization") == f"Basic {expected}"

    async def _send(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        reason: str,
        body: bytes = b"",
        content_type: str = "application/json",
    ) -> None:
        head = f"HTTP/1.1 {status} {reason}\r\nContent-Type: {content_type}\r\n"
        if body or status != 204:
            head += f"Content-Length: {len(body)}\r\n"
        head += "Connection: close\r\n\r\n"
        writer.write(head.encode("ascii"))
        if body:
            writer.write(body)
        await writer.drain()
        writer.close()

    async def _route(
        self, record: dict[str, Any], writer: asyncio.StreamWriter, body: bytes
    ) -> None:
        await self._send(writer, 404, "Not Found", b"{}")
