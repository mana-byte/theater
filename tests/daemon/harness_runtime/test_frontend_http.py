"""HTTP peers share request correlation without replaying commands across polls."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from tests.support.waiting import wait_until
from theater.daemon.harness_runtime.frontend import FrontendRuntimeHost
from theater.daemon.harness_runtime.frontend_http import (
    HTTP_BODY_MAX_BYTES,
    HttpFrontendConnection,
    read_exchange,
)
from theater.harness.contracts.runtime import RuntimeConnectionClosed, RuntimeRequestTimeout


async def _exchange(path, *, token="secret", generation=1, messages=()):
    reader, writer = await asyncio.open_unix_connection(path)
    body = json.dumps(
        {
            "type": "hello",
            "protocol": "theater-frontend-v1",
            "token": token,
            "generation": generation,
            "messages": messages,
        }
    ).encode()
    writer.write(
        b"POST /exchange HTTP/1.1\r\nContent-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )
    await writer.drain()
    try:
        response = await asyncio.wait_for(reader.read(), 5)
    finally:
        writer.close()
        await writer.wait_closed()
    header, _, payload = response.partition(b"\r\n\r\n")
    return int(header.split()[1]), json.loads(payload)


@pytest.fixture
async def listener(monkeypatch):
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        root = Path(directory)
        monkeypatch.setattr(
            "theater.daemon.harness_runtime.frontend.backend_artifacts_dir", lambda _: root
        )
        host = FrontendRuntimeHost()
        peers, gone = [], []

        async def connect(peer):
            peers.append(peer)
            await peer.request("snapshot", {}, timeout=3)

        async def disconnect(peer):
            gone.append(peer)

        await host.start(
            participant_id="p",
            generation=1,
            endpoint="unix://" + str(root / "frontend.sock"),
            token="secret",
            on_connect=connect,
            on_disconnect=disconnect,
        )
        yield host, root / "frontend.sock", peers, gone
        await host.aclose()


async def test_http_handshake_responses_and_generation_fence(listener):
    host, path, peers, gone = listener
    assert (await _exchange(path, token="wrong"))[0] == 409
    assert not peers
    status, first = await _exchange(path)
    assert status == 200
    frame = first["request"]
    assert frame["method"] == "snapshot" and frame["deadline_ms"] > 0
    await _exchange(path, messages=[{"type": "response", "id": frame["id"], "result": {}}])
    await host.wait_ready("p", timeout=3)
    assert len(peers) == 1
    old = peers[0]
    pending = asyncio.create_task(old.request("send", {"text": "once"}, timeout=3))
    _, sent = await _exchange(path)
    assert sent["request"]["method"] == "send"
    # Once dequeued, a lost HTTP response must not put the mutation back on the wire.
    assert (await _exchange(path))[1]["request"] is None
    _, replacement = await _exchange(path, generation=2)
    assert replacement["request"]["method"] == "snapshot"
    with pytest.raises(RuntimeConnectionClosed):
        await pending
    assert old.closed and old in gone
    assert (await _exchange(path, generation=1))[0] == 409
    assert len(peers) == 2


async def test_http_lease_loss_releases_runtime(listener, monkeypatch):
    monkeypatch.setattr("theater.daemon.harness_runtime.frontend_http.HTTP_LEASE_SECONDS", 0.05)
    host, path, peers, gone = listener
    _, first = await _exchange(path)
    await _exchange(
        path, messages=[{"type": "response", "id": first["request"]["id"], "result": {}}]
    )
    await wait_until(lambda: bool(gone), what="expired frontend lease")
    assert gone == peers and peers[0].closed
    with pytest.raises(TimeoutError):
        await host.wait_ready("p", timeout=0.02)


async def test_http_expired_requests_are_not_delivered():
    peer = HttpFrontendConnection(1)
    with pytest.raises(RuntimeRequestTimeout):
        await peer.request("send", {}, timeout=0.01)
    with pytest.raises(TimeoutError):
        await peer.next_request()
    await peer.aclose()


@pytest.mark.parametrize(
    "headers,body",
    [
        (b"Content-Length: 1\r\nTransfer-Encoding: chunked\r\n", b"0"),
        (b"Content-Length: 1\r\nContent-Length: 1\r\n", b"0"),
        (f"Content-Length: {HTTP_BODY_MAX_BYTES + 1}\r\n".encode(), b""),
        (b"Content-Length: 2\r\n", b"[]"),
        (b"Content-Length: 20\r\n", b'{"generation": true}'),
    ],
)
async def test_http_rejects_ambiguous_or_unbounded_input(headers, body):
    reader = asyncio.StreamReader()
    reader.feed_data(b"Content-Type: application/json\r\n" + headers + b"\r\n" + body)
    reader.feed_eof()
    with pytest.raises((ValueError, TypeError, asyncio.IncompleteReadError)):
        await read_exchange(reader, b"POST /exchange HTTP/1.1\r\n")
