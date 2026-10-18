"""The OpenCode HTTP/SSE client against a real loopback fake server.

No mocking of the transport: a real TCP server streams real bytes, exercises
auth, chunked bodies, disconnects, and SSE boundaries — the write-ambiguity
and redaction rules are only meaningful over a socket.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from theater import paths
from theater.harness.builtin.plugins.opencode import http as opencode_http
from theater.harness.builtin.plugins.opencode.http import (
    BASIC_USERNAME,
    OpenCodeClient,
    OpenCodeHttpError,
    OpenCodeStreamError,
    read_client_secret,
    validate_loopback_endpoint,
)

PASSWORD = "test-password-1234"


class FakeServer:
    """A minimal HTTP/1.1 + SSE server that records every request verbatim."""

    def __init__(self, *, password: str = PASSWORD) -> None:
        self._password = password
        self._server: asyncio.AbstractServer | None = None
        self.port = 0
        self.requests: list[dict[str, Any]] = []
        self._behaviors: dict[str, Any] = {}
        self._handler: Any = None

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
            await self._server.wait_closed()

    def set_behavior(self, name: str, value: Any) -> None:
        self._behaviors[name] = value

    async def _route_event(self, writer: asyncio.StreamWriter) -> None:
        behavior = self._behaviors.get("event")
        if behavior == "stall-handshake":
            await asyncio.sleep(30)
            return
        if behavior == "wrong-type":
            await self._send(writer, 200, "OK", b"", content_type="application/json")
            return
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n"
        )
        if behavior == "malformed":
            writer.write(b"data: {not json}\n\n")
        elif behavior == "oversized":
            writer.write(b"data: " + b"x" * 70_000 + b"\n\n")
        elif behavior == "events-then-close":
            for event in ({"type": "server.connected"}, {"type": "session.idle"}):
                writer.write(b"data: " + json.dumps(event).encode() + b"\n\n")
            writer.write(b": keepalive\n")
            await writer.drain()
            writer.close()
            return
        else:
            for event in (
                {"type": "server.connected"},
                {"type": "session.updated"},
                {"type": "session.idle"},
            ):
                writer.write(b"data: " + json.dumps(event).encode() + b"\n\n")
            await writer.drain()
            await asyncio.sleep(30)
        await writer.drain()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
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
        if record["path"] == "/event":
            await self._route_event(writer)
            return
        if record["path"] == "/global/health":
            await self._route_health(writer)
            return
        await self._route_control(record, writer)

    async def _route_health(self, writer: asyncio.StreamWriter) -> None:
        behavior = self._behaviors.get("health")
        if behavior == "plain-text":
            await self._send(writer, 200, "OK", b"healthy", content_type="text/plain")
            return
        if behavior == "chunked":
            payload = json.dumps({"healthy": True, "version": "1.18.29"}).encode()
            await self._send(writer, 200, "OK", payload, chunked=True)
            return
        if behavior == "redirect":
            writer.write(
                b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/x\r\n"
                b"Content-Length: 0\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            return
        payload = json.dumps({"healthy": True, "version": "1.18.29"}).encode()
        await self._send(writer, 200, "OK", payload)

    async def _route_control(self, record: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        path = record["path"]
        if path == "/session" and record["method"] == "POST":
            payload = json.dumps({"id": "ses_proof_1"}).encode()
            await self._send(writer, 200, "OK", payload)
            return
        if path == "/session/status":
            payload = json.dumps({"ses_proof_1": {"status": "idle"}}).encode()
            await self._send(writer, 200, "OK", payload)
            return
        if path == "/session/ses_proof_1":
            payload = json.dumps({"id": "ses_proof_1", "title": "t"}).encode()
            await self._send(writer, 200, "OK", payload)
            return
        if path == "/session/ses_proof_1/prompt_async":
            if self._behaviors.get("prompt") == "write-then-drop":
                writer.close()
                return
            await self._send(writer, 204, "No Content", b"")
            return
        if path == "/session/ses_proof_1/abort":
            await self._send(writer, 200, "OK", b"true")
            return
        await self._send(writer, 404, "Not Found", b"{}")


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


def _client(server: FakeServer, token_file: Path) -> OpenCodeClient:
    return OpenCodeClient(endpoint=server.endpoint, token_file=token_file)


# ---- endpoint + credential hygiene ----------------------------------


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


# ---- named surfaces ---------------------------------------------------


async def test_health_create_status_and_abort(server: FakeServer, token_file: Path) -> None:
    client = _client(server, token_file)
    assert await client.health() == "1.18.29"
    assert await client.create_session(title="t") == "ses_proof_1"
    assert (await client.read_session("ses_proof_1"))["id"] == "ses_proof_1"
    session_status = (await client.session_status())["ses_proof_1"]
    assert isinstance(session_status, Mapping)
    assert session_status["status"] == "idle"
    assert await client.abort("ses_proof_1") is True
    assert all(request["headers"].get("authorization") for request in server.requests)


async def test_wrong_password_is_rejected(server: FakeServer, token_file: Path) -> None:
    wrong = token_file.with_name("wrong.token")
    wrong.write_text("not-the-password")
    wrong.chmod(0o600)
    client = OpenCodeClient(endpoint=server.endpoint, token_file=wrong)
    with pytest.raises(OpenCodeHttpError) as raised:
        await client.health()
    assert raised.value.status == 401
    assert "not-the-password" not in str(raised.value)


async def test_prompt_async_accepts_204_no_body(server: FakeServer, token_file: Path) -> None:
    client = _client(server, token_file)
    body = {"messageID": "op_1", "parts": [{"type": "text", "text": "hello proof"}]}
    assert await client.prompt_async("ses_proof_1", body) is None
    prompt = next(r for r in server.requests if r["path"].endswith("prompt_async"))
    assert prompt["method"] == "POST"
    assert json.loads(prompt["body"]) == body


async def test_post_write_ambiguity_is_flagged_and_redacted(
    server: FakeServer, token_file: Path
) -> None:
    server.set_behavior("prompt", "write-then-drop")
    client = _client(server, token_file)
    with pytest.raises(OpenCodeHttpError) as raised:
        await client.prompt_async("ses_proof_1", {"parts": []})
    assert raised.value.written is True, "bytes crossed; the runtime must treat it as UNKNOWN"
    assert "parts" not in str(raised.value)


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

    monkeypatch.setattr(client, "_connect", connect)
    with pytest.raises(OpenCodeHttpError) as raised:
        await client.health()
    assert raised.value.written is True


async def test_connect_refusal_is_unwritten_and_redacted(token_file: Path) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        reserved = probe.getsockname()[1]
    client = OpenCodeClient(endpoint=f"http://127.0.0.1:{reserved}", token_file=token_file)
    with pytest.raises(OpenCodeHttpError) as raised:
        await client.health()
    assert raised.value.written is False
    assert PASSWORD not in str(raised.value)


async def test_redirect_and_wrong_content_type_fail_closed(
    server: FakeServer, token_file: Path
) -> None:
    client = _client(server, token_file)
    server.set_behavior("health", "redirect")
    with pytest.raises(OpenCodeHttpError, match="redirect refused"):
        await client.health()
    server.set_behavior("health", "plain-text")
    with pytest.raises(OpenCodeHttpError, match="not application/json"):
        await client.health()
    server.set_behavior("health", "chunked")
    assert await client.health() == "1.18.29"


async def test_missing_session_answer_fails_closed(server: FakeServer, token_file: Path) -> None:
    client = _client(server, token_file)
    with pytest.raises(OpenCodeHttpError) as raised:
        await client.prompt_async("ses_missing", {"parts": []})
    assert raised.value.status == 404
    assert raised.value.session_id == "ses_missing"


@pytest.mark.parametrize("session_id", ["", "../x", "a/b", "a" * 129, "hello%2Fworld"])
async def test_session_ids_are_bounded_path_tokens(
    server: FakeServer, token_file: Path, session_id: str
) -> None:
    client = _client(server, token_file)
    with pytest.raises(ValueError, match="path-safe"):
        await client.prompt_async(session_id, {"parts": []})
    assert server.requests == []


async def test_request_body_is_bounded_before_connect(server: FakeServer, token_file: Path) -> None:
    client = _client(server, token_file)
    with pytest.raises(OpenCodeHttpError) as raised:
        await client.prompt_async("ses_proof_1", {"text": "x" * 1_048_577})
    assert raised.value.written is False
    assert server.requests == []


# ---- SSE --------------------------------------------------------------


async def test_events_stream_until_server_closes(server: FakeServer, token_file: Path) -> None:
    server.set_behavior("event", "events-then-close")
    client = _client(server, token_file)
    seen: list[str] = []
    with pytest.raises(OpenCodeStreamError):
        async for event in client.events():
            seen.append(str(event["type"]))
    assert seen == ["server.connected", "session.idle"]


async def test_event_subscription_requires_event_stream_content_type(
    server: FakeServer, token_file: Path
) -> None:
    server.set_behavior("event", "wrong-type")
    client = _client(server, token_file)
    with pytest.raises(OpenCodeStreamError, match="not text/event-stream"):
        async for _ in client.events():
            pass


async def test_event_handshake_has_one_total_deadline(
    server: FakeServer, token_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server.set_behavior("event", "stall-handshake")
    monkeypatch.setattr(opencode_http, "REQUEST_DEADLINE_SECONDS", 0.05)
    client = _client(server, token_file)
    with pytest.raises(OpenCodeStreamError, match="subscription failed"):
        async for _ in client.events():
            pass


async def test_full_event_queue_drains_before_stream_failure(
    server: FakeServer, token_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(opencode_http, "MAX_SSE_QUEUE", 1)
    server.set_behavior("event", "events-then-close")
    client = _client(server, token_file)
    seen: list[str] = []
    with pytest.raises(OpenCodeStreamError):
        async for event in client.events():
            seen.append(str(event["type"]))
    assert seen == ["server.connected", "session.idle"]


async def test_malformed_and_oversized_events_fail_closed(
    server: FakeServer, token_file: Path
) -> None:
    client = _client(server, token_file)
    server.set_behavior("event", "malformed")
    with pytest.raises(OpenCodeStreamError, match="malformed SSE event"):
        async for _ in client.events():
            pass
    server.set_behavior("event", "oversized")
    with pytest.raises(OpenCodeStreamError, match="oversized"):
        async for _ in client.events():
            pass


async def test_abandoned_subscription_cleans_up(server: FakeServer, token_file: Path) -> None:
    client = _client(server, token_file)
    stream = client.events()
    first = await stream.__anext__()
    assert first["type"] == "server.connected"
    await stream.aclose()
    await asyncio.sleep(0.1)
