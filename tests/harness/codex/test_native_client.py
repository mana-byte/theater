"""Codex native WebSocket clients: framing, handshake and bootstrap evidence predicates."""

from __future__ import annotations

import base64
import json
import os
import socket
import threading
import uuid
from pathlib import Path

import pytest

from tests.native.codex_native_client import (
    MAX_FRAME_BYTES,
    FrameTooLarge,
    NativeWebSocketClient,
    decode_frames,
    encode_frame,
    expected_accept,
)
from tests.support.codex_bootstrap_proof import (
    client,
    discover_ui_thread,
    ui_ready_evidence,
)


class TestFramingCodec:
    def test_roundtrip_all_length_classes(self) -> None:
        for size in (0, 5, 125, 126, 65535, 65536, 100_000):
            payload = os.urandom(size)
            frame = encode_frame(payload, mask=True)
            assert frame[1] & 0x80, "client frames must be masked"
            frames, remainder = decode_frames(frame)
            assert remainder == b""
            assert len(frames) == 1
            opcode, decoded = frames[0]
            assert opcode == 0x1
            assert decoded == payload

    def test_multiple_frames_in_one_buffer(self) -> None:
        buffer = encode_frame(b'{"a": 1}', mask=False) + encode_frame(b'{"b": 2}', mask=False)
        buffer += b"partial"
        frames, remainder = decode_frames(buffer)
        assert [payload for _, payload in frames] == [b'{"a": 1}', b'{"b": 2}']
        assert remainder == b"partial"

    def test_oversized_frame_rejected(self) -> None:
        header = bytearray([0x81, 0x80 | 127]) + (MAX_FRAME_BYTES + 1).to_bytes(8, "big")
        with pytest.raises(FrameTooLarge):
            decode_frames(bytes(header))

    def test_client_handshake_shape(self) -> None:
        from tests.native.codex_native_client import build_client_handshake

        request, key = build_client_handshake()
        text = request.decode()
        assert text.startswith("GET / HTTP/1.1\r\n")
        assert "Upgrade: websocket\r\n" in text
        assert "Connection: Upgrade\r\n" in text
        assert f"Sec-WebSocket-Key: {key}\r\n" in text
        assert "Sec-WebSocket-Version: 13\r\n" in text
        base64.b64decode(key)  # the key must be valid base64


class TestClientAgainstFakeServer:
    """Exercise NativeWebSocketClient offline against a scripted server."""

    @pytest.fixture()
    def fake_server(self):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        path = Path(f"/tmp/theater-proof-fake-{os.getpid()}-{uuid.uuid4().hex[:6]}.sock")
        listener.bind(str(path))
        listener.listen(1)
        received: list[dict] = []
        pongs: list[bytes] = []
        got_request = threading.Event()
        got_pong = threading.Event()

        def serve() -> None:
            conn, _ = listener.accept()
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(4096)
            accept = expected_accept(data.decode().split("Sec-WebSocket-Key: ")[1].split("\r\n")[0])
            conn.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "connection: Upgrade\r\n"
                    "upgrade: websocket\r\n"
                    f"sec-websocket-accept: {accept}\r\n\r\n"
                ).encode()
            )
            # A ping the client must answer. The response is withheld until
            # the pong arrives: answering and closing earlier would race a
            # client that still owes its pong (same-process thread
            # scheduling can run this server between the client's request
            # send and its pong send, and the pong then hits a closed
            # socket). Waiting also makes the pong an explicit requirement
            # — a client that never answers the ping gets a request
            # timeout instead of a response.
            conn.sendall(encode_frame(b"ping-payload", 0x9, mask=False))
            buffer = b""
            while not (got_request.is_set() and got_pong.is_set()):
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buffer += chunk
                frames, buffer = decode_frames(buffer)
                for opcode, payload in frames:
                    if opcode == 0xA:
                        pongs.append(payload)
                        got_pong.set()
                    elif opcode in (0x1, 0x2):
                        received.append(json.loads(payload))
                        got_request.set()
            conn.sendall(encode_frame(b'{"id": 1, "result": {"ok": true}}', mask=False))
            conn.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        yield path, received, pongs
        listener.close()
        path.unlink(missing_ok=True)

    def test_handshake_request_and_framing(self, fake_server) -> None:
        path, received, pongs = fake_server
        client = NativeWebSocketClient(path)
        assert client.handshake.status_line == "HTTP/1.1 101 Switching Protocols"
        assert client.handshake.accept_valid is True
        assert client.handshake.headers["upgrade"] == "websocket"
        response = client.request("initialize", {"clientInfo": {"name": "x", "version": "0.1"}})
        assert response == {"id": 1, "result": {"ok": True}}
        assert received and received[0]["method"] == "initialize"
        assert "jsonrpc" not in received[0]
        # The ping was answered exactly once, echoing the ping payload — the
        # response above was withheld until this pong arrived.
        assert pongs == [b"ping-payload"]
        client.close()


def test_client_text_frames_are_masked_and_round_trip() -> None:
    for size in (0, 5, 125, 300, 70_000):
        payload = bytes(range(256))[: min(size, 256)] * (size // 256 + 1)
        payload = payload[:size]
        mask = b"\x0f\x1e\x2d\x3c"
        frame = client.encode_client_text_frame(payload, mask)
        assert frame[0] == 0x81  # FIN + text
        assert frame[1] & 0x80, "client frames must set the mask bit"
        length = frame[1] & 0x7F
        offset = 2
        if length == 126:
            length = int.from_bytes(frame[2:4], "big")
            offset = 4
        elif length == 127:
            length = int.from_bytes(frame[2:10], "big")
            offset = 10
        assert length == size
        body = frame[offset:]
        assert len(body) == 4 + size
        unmasked = bytes(byte ^ body[i % 4] for i, byte in enumerate(body[4:]))
        assert unmasked == payload


def test_decode_server_frame_small_medium_large() -> None:
    for size in (3, 200, 70_000):
        payload = b"x" * size
        wire = _server_frame(payload)
        frame = client.decode_server_frame(wire)
        assert frame is not None
        assert frame.fin and frame.opcode == client.OP_TEXT
        assert frame.payload == payload
        assert client._frame_consumed(wire) == len(wire)


def test_decode_server_frame_waits_for_complete_header() -> None:
    payload = b"hello"
    wire = _server_frame(payload)
    for cut in range(len(wire)):
        assert client.decode_server_frame(wire[:cut]) is None
        assert client._frame_consumed(wire[:cut]) is None


def test_upgrade_request_carries_required_headers_and_no_origin() -> None:
    key = base64.b64encode(b"0123456789abcdef").decode()
    request = client.build_upgrade_request("/", key).decode("ascii")
    lines = request.split("\r\n")
    assert lines[0] == "GET / HTTP/1.1"
    assert not any(line.lower().startswith("origin:") for line in lines)
    headers = {line.split(":")[0].lower(): line.split(":", 1)[1].strip() for line in lines[1:-2]}
    assert headers["upgrade"] == "websocket"
    assert headers["connection"] == "Upgrade"
    assert headers["sec-websocket-version"] == "13"
    assert headers["sec-websocket-key"] == key


def test_expected_accept_header_matches_rfc_6455_vector() -> None:
    # RFC 6455 §1.3 example handshake.
    assert client.expected_accept_header("dGhlIHNhbXBsZSBub25jZQ==") == (
        "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
    )


def test_discover_ui_thread_requires_exactly_one_broadcast_for_cwd() -> None:
    thread = {"id": "019da1a1-bed9-7a43-88a2-b49d43915021", "cwd": "/tmp/repo"}

    def notification(method, payload):
        return client.Received(kind="notification", method=method, id=None, payload=payload)

    received = [
        notification("thread/tokenUsage/updated", {"threadId": "other"}),
        notification("thread/started", {"thread": thread}),
    ]
    assert discover_ui_thread(received, cwd="/tmp/repo") == thread
    with pytest.raises(AssertionError, match="exactly one thread/started"):
        discover_ui_thread(received, cwd="/tmp/different")
    duplicate = notification("thread/started", {"thread": dict(thread)})
    with pytest.raises(AssertionError, match="exactly one thread/started"):
        discover_ui_thread([*received, duplicate], cwd="/tmp/repo")


def test_ui_ready_evidence_requires_a_live_broadcast_status() -> None:
    thread = {"id": "t1", "status": {"type": "idle"}}
    assert ui_ready_evidence(thread) == [
        "thread/started broadcast delivered the exact id t1",
        "thread state is live: idle",
    ]
    with pytest.raises(AssertionError, match="not in a live state"):
        ui_ready_evidence({"id": "t1", "status": {"type": "notLoaded"}})


def _server_frame(payload: bytes) -> bytes:
    """Unmasked server text frame, the shape the app-server sends."""
    length = len(payload)
    if length < 126:
        header = bytes([0x81, length])
    elif length < 1 << 16:
        header = bytes([0x81, 126]) + length.to_bytes(2, "big")
    else:
        header = bytes([0x81, 127]) + length.to_bytes(8, "big")
    return header + payload
