"""Supported OpenCode routes over a bounded, credentialed loopback transport."""

from __future__ import annotations

import asyncio
import base64
import json
import socket
from pathlib import Path
from typing import Any

import pytest

from theater import paths
from theater.harness.builtin.plugins.opencode import http as opencode_http
from theater.harness.builtin.plugins.opencode.http import (
    BASIC_USERNAME,
    OpenCodeHttpError,
    read_client_secret,
    validate_loopback_endpoint,
)
from theater.harness.builtin.plugins.opencode.http_v2 import OpenCodeV2Client
from theater.harness.builtin.plugins.opencode.runtime_plan import VERSION_POLICY

# A real loopback server, no transport mocking: write ambiguity and redaction only mean
# something over a socket.

PASSWORD = "test-password-1234"


class FakeServer:
    """A minimal 2.x HTTP server that records requests and injected transport failures."""

    def __init__(self, *, password: str = PASSWORD) -> None:
        self._password = password
        self._server: asyncio.AbstractServer | None = None
        self.port = 0
        self.requests: list[dict[str, Any]] = []
        self._behaviors: dict[str, Any] = {}
        self._connections: set[asyncio.Task[Any]] = set()

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
            # Stalled handlers sleep for 30 s; cancel them so wait_closed returns at once.
            connections = tuple(self._connections)
            for task in connections:
                task.cancel()
            await asyncio.gather(*connections, return_exceptions=True)
            await self._server.wait_closed()

    def set_behavior(self, name: str, value: Any) -> None:
        self._behaviors[name] = value

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._connections.add(task)
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            parts = request_line.decode("latin-1").rstrip("\r\n").split(" ")
            headers: dict[str, str] = {}
            body = b""
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n"):
                    break
                name, _, value = line.decode("latin-1").rstrip("\r\n").partition(":")
                headers[name.strip().lower()] = value.strip()
            if "content-length" in headers:
                body = await reader.readexactly(int(headers["content-length"]))
            record = {
                "method": parts[0],
                "path": parts[1] if len(parts) > 1 else "",
                "headers": headers,
                "body": body,
            }
            self.requests.append(record)
            await self._route(record, writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self._connections.discard(task)
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
        chunked: bool = False,
        extra: str = "",
    ) -> None:
        head = f"HTTP/1.1 {status} {reason}\r\nContent-Type: {content_type}\r\n{extra}"
        if chunked:
            head += "Transfer-Encoding: chunked\r\n"
        elif body or status not in (204,):
            head += f"Content-Length: {len(body)}\r\n"
        head += "Connection: close\r\n\r\n"
        writer.write(head.encode("ascii"))
        if chunked:
            for index in range(0, len(body), 32):
                piece = body[index : index + 32]
                writer.write(f"{len(piece):x}\r\n".encode("ascii") + piece + b"\r\n")
            writer.write(b"0\r\n\r\n")
        elif body:
            writer.write(body)
        await writer.drain()

    async def _route(self, record: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        if not self._authorized(record):
            await self._send(writer, 401, "Unauthorized", b"{}")
            return
        if record["path"] == "/api/info":
            await self._route_info(writer)
            return
        await self._route_control(record, writer)

    async def _route_info(self, writer: asyncio.StreamWriter) -> None:
        behavior = self._behaviors.get("info")
        if behavior == "stall":
            await asyncio.Event().wait()
        if behavior == "oversized":
            await self._send(writer, 200, "OK", b"x" * (opencode_http.MAX_RESPONSE_BYTES + 1))
            return
        if behavior == "plain-text":
            await self._send(writer, 200, "OK", b"healthy", content_type="text/plain")
            return
        if behavior == "chunked":
            payload = json.dumps({"version": VERSION_POLICY.minimum}).encode()
            await self._send(writer, 200, "OK", payload, chunked=True)
            return
        if behavior == "redirect":
            writer.write(
                b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/x\r\n"
                b"Content-Length: 0\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            return
        payload = json.dumps({"version": VERSION_POLICY.minimum}).encode()
        await self._send(writer, 200, "OK", payload)

    async def _route_control(self, record: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        path = record["path"]
        result: dict[str, object]
        if path == "/api/session" and record["method"] == "POST":
            result = {"data": {"id": "ses_one"}}
        elif path == "/api/session/active":
            result = {"data": {}}
        elif path == "/api/session/ses_one":
            result = {"data": {"id": "ses_one", "location": {"directory": "/work"}}}
        elif path == "/api/session/ses_one/prompt":
            if self._behaviors.get("prompt") == "write-then-drop":
                writer.close()
                return
            result = {"data": {"id": json.loads(record["body"])["id"], "time": {"created": 1000}}}
        else:
            await self._send(writer, 404, "Not Found", b"{}")
            return
        await self._send(writer, 200, "OK", json.dumps(result).encode())


@pytest.fixture
def token_file() -> Path:
    path = paths.ensure_home() / "participants" / "h00000000001" / "launch" / "runtime.token"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PASSWORD)
    path.chmod(0o600)
    return path


@pytest.fixture
async def server():
    fake = FakeServer()
    await fake.start()
    yield fake
    await fake.stop()


def _client(server: FakeServer, token_file: Path) -> OpenCodeV2Client:
    return OpenCodeV2Client(endpoint=server.endpoint, token_file=token_file)


def test_endpoint_validation_accepts_only_bare_loopback() -> None:
    assert validate_loopback_endpoint("http://127.0.0.1:4096") == ("127.0.0.1", 4096)
    assert validate_loopback_endpoint("http://[::1]:4096") == ("::1", 4096)
    for bad in (
        "http://localhost:1/",
        "https://127.0.0.1:1",
        "http://0.0.0.0:1",
        "http://example.com:1",
        "http://user:pw@127.0.0.1:1",
        "http://127.0.0.1:1/path",
        "http://127.0.0.1:1?q=1",
        "http://127.0.0.1:0",
        "http://127.0.0.1:70000",
    ):
        with pytest.raises(ValueError):
            validate_loopback_endpoint(bad)


def test_secret_file_refuses_insecure_representations(token_file: Path) -> None:
    assert read_client_secret(token_file).basic_header() == (
        "Basic " + base64.b64encode(f"{BASIC_USERNAME}:{PASSWORD}".encode()).decode("ascii")
    )
    permissive = token_file.with_name("permissive.token")
    permissive.write_text(PASSWORD)
    permissive.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        read_client_secret(permissive)
    linked = token_file.with_name("linked.token")
    linked.symlink_to(token_file)
    with pytest.raises(ValueError, match="unreadable"):
        read_client_secret(linked)
    empty = token_file.with_name("empty.token")
    empty.write_text("")
    empty.chmod(0o600)
    with pytest.raises(ValueError, match="bounded token"):
        read_client_secret(empty)


async def test_version_create_read_and_active(server: FakeServer, token_file: Path) -> None:
    client = _client(server, token_file)
    assert await client.version() == VERSION_POLICY.minimum
    assert await client.create_session(directory="/work") == "ses_one"
    assert (await client.read_session("ses_one"))["id"] == "ses_one"
    assert await client.active() == {}
    assert all(request["headers"].get("authorization") for request in server.requests)


async def test_wrong_password_is_rejected(server: FakeServer, token_file: Path) -> None:
    wrong = token_file.with_name("wrong.token")
    wrong.write_text("not-the-password")
    wrong.chmod(0o600)
    client = OpenCodeV2Client(endpoint=server.endpoint, token_file=wrong)
    with pytest.raises(OpenCodeHttpError) as raised:
        await client.version()
    assert raised.value.status == 401
    assert "not-the-password" not in str(raised.value)


async def test_prompt_admission_carries_exact_message_and_delivery(
    server: FakeServer, token_file: Path
) -> None:
    client = _client(server, token_file)
    admitted = await client.prompt(
        "ses_one", message_id="msg_one", text="hello", delivery="followUp"
    )
    assert admitted == {"id": "msg_one", "time": {"created": 1000}}
    prompt = server.requests[-1]
    assert (prompt["method"], prompt["path"]) == ("POST", "/api/session/ses_one/prompt")
    assert json.loads(prompt["body"]) == {"id": "msg_one", "text": "hello", "delivery": "followUp"}


async def test_post_write_ambiguity_is_flagged_and_redacted(
    server: FakeServer, token_file: Path
) -> None:
    server.set_behavior("prompt", "write-then-drop")
    client = _client(server, token_file)
    with pytest.raises(OpenCodeHttpError) as raised:
        await client.prompt(
            "ses_one", message_id="msg_one", text="private prompt", delivery="followUp"
        )
    assert raised.value.written is True, "bytes crossed; the runtime must treat it as UNKNOWN"
    assert "private prompt" not in str(raised.value)
    assert PASSWORD not in str(raised.value)


async def test_drain_failure_after_write_is_ambiguous(
    server: FakeServer, token_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Writer:
        def write(self, data: bytes) -> None:
            assert data.startswith(b"GET ")

        async def drain(self) -> None:
            raise ConnectionResetError("after write")

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    client = _client(server, token_file)

    async def connect():
        return asyncio.StreamReader(), Writer()

    monkeypatch.setattr(client._transport, "_connect", connect)
    with pytest.raises(OpenCodeHttpError) as raised:
        await client.version()
    assert raised.value.written is True


async def test_connect_refusal_is_unwritten_and_redacted(token_file: Path) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        reserved = probe.getsockname()[1]
    client = OpenCodeV2Client(endpoint=f"http://127.0.0.1:{reserved}", token_file=token_file)
    with pytest.raises(OpenCodeHttpError) as raised:
        await client.version()
    assert raised.value.written is False
    assert PASSWORD not in str(raised.value)


async def test_redirect_and_wrong_content_type_fail_closed(
    server: FakeServer, token_file: Path
) -> None:
    client = _client(server, token_file)
    server.set_behavior("info", "redirect")
    with pytest.raises(OpenCodeHttpError, match="redirect refused"):
        await client.version()
    server.set_behavior("info", "plain-text")
    with pytest.raises(OpenCodeHttpError, match="not application/json"):
        await client.version()
    server.set_behavior("info", "chunked")
    assert await client.version() == VERSION_POLICY.minimum


async def test_missing_session_answer_fails_closed(server: FakeServer, token_file: Path) -> None:
    client = _client(server, token_file)
    with pytest.raises(OpenCodeHttpError) as raised:
        await client.prompt("ses_missing", message_id="msg_one", text="hello", delivery="followUp")
    assert raised.value.status == 404
    assert raised.value.session_id == "ses_missing"


@pytest.mark.parametrize("session_id", ["", "../x", "a/b", "a" * 129, "hello%2Fworld"])
async def test_session_ids_are_bounded_path_tokens(
    server: FakeServer, token_file: Path, session_id: str
) -> None:
    client = _client(server, token_file)
    with pytest.raises(ValueError, match="path-safe"):
        await client.prompt(session_id, message_id="msg_one", text="hello", delivery="followUp")
    assert server.requests == []


async def test_request_body_is_bounded_before_connect(server: FakeServer, token_file: Path) -> None:
    client = _client(server, token_file)
    with pytest.raises(OpenCodeHttpError) as raised:
        await client.prompt(
            "ses_one", message_id="msg_one", text="x" * 1_048_577, delivery="followUp"
        )
    assert raised.value.written is False
    assert server.requests == []


async def test_response_body_is_bounded(server: FakeServer, token_file: Path) -> None:
    server.set_behavior("info", "oversized")
    with pytest.raises(OpenCodeHttpError, match="response body exceeds"):
        await _client(server, token_file).version()


async def test_request_has_one_total_deadline(server, token_file, monkeypatch) -> None:
    server.set_behavior("info", "stall")
    monkeypatch.setattr(opencode_http, "REQUEST_DEADLINE_SECONDS", 0.05)
    with pytest.raises(OpenCodeHttpError, match="deadline exceeded") as raised:
        await _client(server, token_file).version()
    assert raised.value.written is True
