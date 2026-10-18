"""A fake opencode HTTP server with the password it expects."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, ClassVar

from tests.support.opencode_releases import V1_RELEASE
from theater.harness.builtin.plugins.opencode.http import (
    BASIC_USERNAME,
)

PASSWORD = "test-password-1234"


_CLOSE_STREAM = object()


class ServerFake:
    """A stock-shaped HTTP/1.1 + SSE server with recorded requests."""

    def __init__(self, *, password: str = PASSWORD) -> None:
        self._password = password
        self._server: asyncio.AbstractServer | None = None
        self.port = 0
        self.requests: list[dict[str, Any]] = []
        self.behaviors: dict[str, str] = {}
        self.sessions: dict[str, dict[str, object]] = {}
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.statuses: dict[str, str] = {}
        self.inputs: dict[str, list[dict[str, object]]] = {"permission": [], "question": []}
        self.sse_events: asyncio.Queue[object] = asyncio.Queue()
        self.streams_opened = 0
        self._next = 0

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        assert self._server.sockets
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        # Never wait_closed(): a live SSE handler keeps a connection open until
        # the test loop cancels it, so waiting would hang teardown.
        if self._server is not None:
            self._server.close()

    def add_session(self, session_id: str) -> None:
        self.sessions[session_id] = {"id": session_id}
        self.messages[session_id] = []

    def push_event(self, event: dict[str, object]) -> None:
        self.sse_events.put_nowait(event)

    def push_idle(self, session_id: str) -> None:
        # Pinned shape: SessionStatus.set deletes idle sessions from the map.
        self.statuses.pop(session_id, None)
        self.push_event({"type": "session.idle", "properties": {"sessionID": session_id}})

    def close_stream(self) -> None:
        self.sse_events.put_nowait(_CLOSE_STREAM)

    def _mint_session(self) -> str:
        self._next += 1
        session_id = f"ses_fake_{self._next}"
        self.add_session(session_id)
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
        if not self._authorized(record):
            await self._send(writer, 401, "Unauthorized", b"{}")
            return
        path = record["path"]
        if path == "/event":
            await self._route_event(writer)
            return
        if path == "/global/health":
            if self.behaviors.get("health") == "fail":
                await self._send(writer, 500, "Internal Server Error", b"{}")
                return
            version = self.behaviors.get("health-version", V1_RELEASE)
            payload = json.dumps({"healthy": True, "version": version}).encode()
            await self._send(writer, 200, "OK", payload)
            return
        segments = [segment for segment in path.split("/") if segment]
        if len(segments) == 1 and segments[0] in self.inputs and record["method"] == "GET":
            await self._send(writer, 200, "OK", json.dumps(self.inputs[segments[0]]).encode())
            return
        if segments == ["session"] and record["method"] == "POST":
            session_id = self._mint_session()
            payload = json.dumps({"id": session_id}).encode()
            await self._send(writer, 200, "OK", payload)
            return
        if segments == ["session", "status"] and record["method"] == "GET":
            await self._route_status(writer)
            return
        if len(segments) >= 2 and segments[0] == "session":
            await self._route_session(writer, segments, segments[1], record, body)
            return
        await self._send(writer, 404, "Not Found", b"{}")

    async def _route_session(
        self,
        writer: asyncio.StreamWriter,
        segments: list[str],
        session_id: str,
        record: dict[str, Any],
        body: bytes,
    ) -> None:
        if segments[2:] == ["children"] and record["method"] == "GET":
            children = [row for row in self.sessions.values() if row.get("parentID") == session_id]
            await self._send(writer, 200, "OK", json.dumps(children).encode())
            return
        if segments[2:] == ["fork"] and record["method"] == "POST":
            if session_id not in self.sessions:
                await self._send(writer, 404, "Not Found", b"{}")
                return
            child = self._mint_session()
            payload = json.dumps({"id": child}).encode()
            await self._send(writer, 200, "OK", payload)
            return
        if segments[2:] == ["message"]:
            if session_id not in self.sessions:
                await self._send(writer, 404, "Not Found", b"{}")
                return
            payload = json.dumps(self.messages[session_id]).encode()
            await self._send(writer, 200, "OK", payload)
            return
        if segments[2:] == ["prompt_async"] and record["method"] == "POST":
            if session_id not in self.sessions:
                await self._send(writer, 404, "Not Found", b"{}")
                return
            await self._route_prompt(writer, session_id, body)
            return
        if len(segments) == 2 and record["method"] == "GET":
            if session_id not in self.sessions:
                payload = json.dumps({"name": "NotFoundError"}).encode()
                await self._send(writer, 404, "Not Found", payload)
                return
            if self.behaviors.get("readback") == "swap":
                payload = json.dumps({"id": "ses_somebody_else"}).encode()
                await self._send(writer, 200, "OK", payload)
                return
            payload = json.dumps(self.sessions[session_id]).encode()
            await self._send(writer, 200, "OK", payload)
            return
        await self._send(writer, 404, "Not Found", b"{}")

    async def _route_status(self, writer: asyncio.StreamWriter) -> None:
        behavior = self.behaviors.get("status")
        if behavior == "fail":
            await self._send(writer, 500, "Internal Server Error", b"{}")
            return
        payload: dict[str, object]
        if behavior == "malformed-top":
            payload = {"kind": "broken"}
        elif behavior == "malformed-target":
            payload = {"ses_parent": 42}
        elif behavior == "unknown-type":
            payload = {"ses_parent": {"type": "bananas"}}
        elif behavior == "foreign":
            payload = {"ses_somebody_else": {"type": "busy"}}
        else:
            payload = {sid: {"type": value} for sid, value in self.statuses.items()}
        await self._send(writer, 200, "OK", json.dumps(payload).encode())

    def _record_user(self, session_id: str, message_id: str, *, echo: bool) -> None:
        self.messages[session_id].append({"info": {"id": message_id, "role": "user"}, "parts": []})
        if echo:
            self.push_event(
                {
                    "type": "message.updated",
                    "properties": {
                        "sessionID": session_id,
                        "info": {"id": message_id, "role": "user"},
                    },
                }
            )

    async def _prompt_drop(
        self, writer: asyncio.StreamWriter, session_id: str, message_id: str | None
    ) -> None:
        writer.close()

    async def _prompt_500(
        self, writer: asyncio.StreamWriter, session_id: str, message_id: str | None
    ) -> None:
        await self._send(writer, 500, "Internal Server Error", b"{}")

    async def _prompt_400(
        self, writer: asyncio.StreamWriter, session_id: str, message_id: str | None
    ) -> None:
        await self._send(writer, 400, "Bad Request", b"{}")

    async def _prompt_body(
        self, writer: asyncio.StreamWriter, session_id: str, message_id: str | None
    ) -> None:
        await self._send(writer, 200, "OK", json.dumps({"queued": True}).encode())

    async def _prompt_404(
        self, writer: asyncio.StreamWriter, session_id: str, message_id: str | None
    ) -> None:
        await self._send(writer, 404, "Not Found", b"{}")

    async def _prompt_fast(
        self, writer: asyncio.StreamWriter, session_id: str, message_id: str | None
    ) -> None:
        # The turn completes before the 204 reaches the client.
        self.statuses.pop(session_id, None)
        if isinstance(message_id, str):
            self._record_user(session_id, message_id, echo=True)
        self.push_event({"type": "session.idle", "properties": {"sessionID": session_id}})
        await asyncio.sleep(0.05)
        await self._send(writer, 204, "No Content", b"")

    async def _prompt_ok_wrong_role(
        self, writer: asyncio.StreamWriter, session_id: str, message_id: str | None
    ) -> None:
        if isinstance(message_id, str):
            self.messages[session_id].append(
                {"info": {"id": message_id, "role": "assistant"}, "parts": []}
            )
        await self._send(writer, 204, "No Content", b"")

    async def _prompt_stale_idle(
        self, writer: asyncio.StreamWriter, session_id: str, message_id: str | None
    ) -> None:
        # A stale idle from a previous turn lands before this turn's echo.
        self.push_event({"type": "session.idle", "properties": {"sessionID": session_id}})
        await asyncio.sleep(0.02)
        if isinstance(message_id, str):
            self._record_user(session_id, message_id, echo=True)
            await asyncio.sleep(0.02)
        self.statuses[session_id] = "busy"
        await self._send(writer, 204, "No Content", b"")

    _PROMPT_BEHAVIORS: ClassVar[dict[str, Any]] = {
        "drop": _prompt_drop,
        "500": _prompt_500,
        "400": _prompt_400,
        "body": _prompt_body,
        "404": _prompt_404,
        "fast": _prompt_fast,
        "ok-wrong-role": _prompt_ok_wrong_role,
        "stale-idle": _prompt_stale_idle,
    }

    async def _route_prompt(
        self, writer: asyncio.StreamWriter, session_id: str, body: bytes
    ) -> None:
        behavior = self.behaviors.get("prompt", "ok")
        try:
            parsed = json.loads(body)
            message_id = parsed["messageID"] if isinstance(parsed, dict) else None
        except ValueError:
            message_id = None
        handler = self._PROMPT_BEHAVIORS.get(behavior)
        if handler is not None:
            await handler(self, writer, session_id, message_id)
            return
        # Admission: the user message becomes durable API state before 204.
        if behavior != "ok-no-record" and isinstance(message_id, str):
            self._record_user(session_id, message_id, echo=behavior == "ok")
        self.push_event(
            {
                "type": "session.status",
                "properties": {"sessionID": session_id, "status": {"type": "busy"}},
            }
        )
        self.statuses[session_id] = "busy"
        await self._send(writer, 204, "No Content", b"")

    async def _route_event(self, writer: asyncio.StreamWriter) -> None:
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n"
        )
        writer.write(
            b"data: "
            + json.dumps({"type": "server.connected", "properties": {}}).encode()
            + b"\n\n"
        )
        await writer.drain()
        self.streams_opened += 1
        while True:
            try:
                event = self.sse_events.get_nowait()
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.005)
                continue
            if event is _CLOSE_STREAM:
                break
            writer.write(b"data: " + json.dumps(event).encode() + b"\n\n")
            try:
                await writer.drain()
            except ConnectionError:
                return
        writer.close()
