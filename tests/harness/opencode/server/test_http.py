"""Supported OpenCode routes over a bounded, credentialed loopback transport."""

from __future__ import annotations

import asyncio
import base64
import json
import socket
from pathlib import Path
from typing import Any

import pytest

from tests.support.opencode_server import PASSWORD, ServerFake
from theater import paths
from theater.harness.builtin.plugins.opencode import http as opencode_http
from theater.harness.builtin.plugins.opencode.http import (
    BASIC_USERNAME,
    OpenCodeHttpError,
    read_client_secret,
    validate_loopback_endpoint,
)
from theater.harness.builtin.plugins.opencode.runtime_plan import VERSION_POLICY
from theater.harness.builtin.plugins.opencode.session_http import OpenCodeSessionClient

# A real loopback server, no transport mocking: write ambiguity and redaction only mean
# something over a socket.


class HttpFake(ServerFake):
    """The transport's routes, plus injected wire-level failures."""

    def __init__(self) -> None:
        super().__init__()
        self._behaviors: dict[str, str] = {}
        self._stall = asyncio.Event()

    def set_behavior(self, name: str, value: str) -> None:
        self._behaviors[name] = value

    async def stop(self) -> None:
        self._stall.set()  # release a stalled handler so the loop can close cleanly
        await super().stop()

    async def _route(
        self, record: dict[str, Any], writer: asyncio.StreamWriter, body: bytes
    ) -> None:
        if not self._authorized(record):
            await self._send(writer, 401, "Unauthorized", b"{}")
        elif record["path"] == "/api/info":
            await self._route_info(writer)
        elif record["path"] == "/api/session/ses_one/prompt" and record["method"] == "POST":
            if self._behaviors.get("prompt") == "write-then-drop":
                writer.close()
                return
            admitted = {"data": {"id": json.loads(body)["id"], "time": {"created": 1000}}}
            await self._send(writer, 200, "OK", json.dumps(admitted).encode())
        else:
            await self._send(writer, 404, "Not Found", b"{}")

    async def _route_info(self, writer: asyncio.StreamWriter) -> None:
        behavior = self._behaviors.get("info")
        if behavior == "stall":
            await self._stall.wait()
        payload = json.dumps({"version": VERSION_POLICY.minimum}).encode()
        if behavior == "oversized":
            await self._send(writer, 200, "OK", b"x" * (opencode_http.MAX_RESPONSE_BYTES + 1))
        elif behavior == "plain-text":
            await self._send(writer, 200, "OK", b"healthy", content_type="text/plain")
        elif behavior == "chunked":
            await self._chunked(writer, payload)
        elif behavior == "redirect":
            writer.write(
                b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/x\r\n"
                b"Content-Length: 0\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
        else:
            await self._send(writer, 200, "OK", payload)

    async def _chunked(self, writer: asyncio.StreamWriter, body: bytes) -> None:
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
        )
        for index in range(0, len(body), 32):
            piece = body[index : index + 32]
            writer.write(f"{len(piece):x}\r\n".encode("ascii") + piece + b"\r\n")
        writer.write(b"0\r\n\r\n")
        await writer.drain()


@pytest.fixture
def token_file() -> Path:
    path = paths.ensure_home() / "participants" / "h00000000001" / "launch" / "runtime.token"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PASSWORD)
    path.chmod(0o600)
    return path


@pytest.fixture
async def server():
    fake = HttpFake()
    await fake.start()
    yield fake
    await fake.stop()


def _client(server: HttpFake, token_file: Path) -> OpenCodeSessionClient:
    return OpenCodeSessionClient(endpoint=server.endpoint, token_file=token_file)


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


async def test_wrong_password_is_rejected(server: HttpFake, token_file: Path) -> None:
    wrong = token_file.with_name("wrong.token")
    wrong.write_text("not-the-password")
    wrong.chmod(0o600)
    client = OpenCodeSessionClient(endpoint=server.endpoint, token_file=wrong)
    with pytest.raises(OpenCodeHttpError) as raised:
        await client.version()
    assert raised.value.status == 401
    assert "not-the-password" not in str(raised.value)


async def test_post_write_ambiguity_is_flagged_and_redacted(
    server: HttpFake, token_file: Path
) -> None:
    server.set_behavior("prompt", "write-then-drop")
    client = _client(server, token_file)
    with pytest.raises(OpenCodeHttpError) as raised:
        await client.prompt(
            "ses_one", message_id="msg_one", text="private prompt", delivery="queue"
        )
    assert raised.value.written is True, "bytes crossed; the runtime must treat it as UNKNOWN"
    assert "private prompt" not in str(raised.value)
    assert PASSWORD not in str(raised.value)


async def test_drain_failure_after_write_is_ambiguous(
    server: HttpFake, token_file: Path, monkeypatch: pytest.MonkeyPatch
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
    client = OpenCodeSessionClient(endpoint=f"http://127.0.0.1:{reserved}", token_file=token_file)
    with pytest.raises(OpenCodeHttpError) as raised:
        await client.version()
    assert raised.value.written is False
    assert PASSWORD not in str(raised.value)


async def test_redirect_and_wrong_content_type_fail_closed(
    server: HttpFake, token_file: Path
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


async def test_missing_session_answer_fails_closed(server: HttpFake, token_file: Path) -> None:
    client = _client(server, token_file)
    with pytest.raises(OpenCodeHttpError) as raised:
        await client.prompt("ses_missing", message_id="msg_one", text="hello", delivery="queue")
    assert raised.value.status == 404
    assert raised.value.session_id == "ses_missing"


@pytest.mark.parametrize("session_id", ["", "../x", "a/b", "a" * 129, "hello%2Fworld"])
async def test_session_ids_are_bounded_path_tokens(
    server: HttpFake, token_file: Path, session_id: str
) -> None:
    client = _client(server, token_file)
    with pytest.raises(ValueError, match="path-safe"):
        await client.prompt(session_id, message_id="msg_one", text="hello", delivery="queue")
    assert server.requests == []


async def test_request_body_is_bounded_before_connect(server: HttpFake, token_file: Path) -> None:
    client = _client(server, token_file)
    with pytest.raises(OpenCodeHttpError) as raised:
        await client.prompt("ses_one", message_id="msg_one", text="x" * 1_048_577, delivery="queue")
    assert raised.value.written is False
    assert server.requests == []


async def test_response_body_is_bounded(server: HttpFake, token_file: Path) -> None:
    server.set_behavior("info", "oversized")
    with pytest.raises(OpenCodeHttpError, match="response body exceeds"):
        await _client(server, token_file).version()


async def test_request_has_one_total_deadline(server, token_file, monkeypatch) -> None:
    server.set_behavior("info", "stall")
    monkeypatch.setattr(opencode_http, "REQUEST_DEADLINE_SECONDS", 0.05)
    with pytest.raises(OpenCodeHttpError, match="deadline exceeded") as raised:
        await _client(server, token_file).version()
    assert raised.value.written is True
