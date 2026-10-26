"""WebSocket-over-Unix runtime transport: RFC 6455 frame codec and client against a real socket."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import itertools
import json
import os
from pathlib import Path

import pytest

from tests.support.waiting import wait_until
from tests.support.ws_server import WsTestServer
from theater.daemon.harness_runtime.constants import RUNTIME_WS_MAX_FRAME_BYTES
from theater.daemon.harness_runtime.errors import (
    RuntimeConnectionSaturated,
    RuntimeHandshakeError,
    RuntimeNotificationOverflow,
    RuntimePayloadTooLarge,
    RuntimeProtocolError,
)
from theater.daemon.harness_runtime.frames import (
    CLOSE_NORMAL,
    OP_CLOSE,
    OP_CONTINUATION,
    OP_PING,
    OP_PONG,
    OP_TEXT,
    FrameDecoder,
    FrameProtocolError,
    encode_frame,
)
from theater.daemon.harness_runtime.transport import (
    WebSocketRuntimeIO,
    wait_for_unix_endpoint,
)
from theater.harness.contracts.runtime import (
    RuntimeConnectionClosed,
    RuntimeConnectionError,
    RuntimeNotification,
    RuntimeRequestError,
    RuntimeRequestTimeout,
)


def _decode_single(data: bytes, *, expect_masked: bool = False) -> tuple[int, bytes, bool]:
    decoder = FrameDecoder(expect_masked=expect_masked, max_frame_bytes=RUNTIME_WS_MAX_FRAME_BYTES)
    frames = decoder.feed(data)
    assert len(frames) == 1
    frame = frames[0]
    return frame.opcode, frame.payload, frame.fin


def test_masked_roundtrip() -> None:
    encoded = encode_frame(OP_TEXT, b'{"id": 1}', mask=True)
    decoder = FrameDecoder(expect_masked=True, max_frame_bytes=RUNTIME_WS_MAX_FRAME_BYTES)
    frames = decoder.feed(encoded)
    assert len(frames) == 1
    assert (frames[0].opcode, frames[0].payload, frames[0].fin) == (
        OP_TEXT,
        b'{"id": 1}',
        True,
    )


@pytest.mark.parametrize(
    ("opcode", "payload"),
    [
        pytest.param(OP_TEXT, b"hello", id="short"),
        pytest.param(OP_PONG, b"", id="empty"),
        pytest.param(OP_TEXT, bytes(range(256)) * 3, id="sixteen_bit_length"),
        pytest.param(OP_TEXT, b"y" * 70_000, id="sixty_four_bit_length"),
    ],
)
def test_unmasked_roundtrip(opcode: int, payload: bytes) -> None:
    decoded_opcode, decoded, _ = _decode_single(encode_frame(opcode, payload, mask=False))
    assert (decoded_opcode, decoded) == (opcode, payload)


def test_mask_bit_is_set_on_client_frames() -> None:
    encoded = encode_frame(OP_TEXT, b"abc", mask=True)
    assert encoded[1] & 0x80, "client frames must set the mask bit"
    server_decoder = FrameDecoder(expect_masked=True, max_frame_bytes=RUNTIME_WS_MAX_FRAME_BYTES)
    frames = server_decoder.feed(encoded)
    assert frames[0].payload == b"abc"
    assert frames[0].masked is True


def test_decode_across_single_byte_feeds() -> None:
    encoded = encode_frame(OP_TEXT, b"x" * 300, mask=True)
    decoder = FrameDecoder(expect_masked=True, max_frame_bytes=RUNTIME_WS_MAX_FRAME_BYTES)
    frames: list[object] = []
    for byte in encoded:
        frames.extend(decoder.feed(bytes([byte])))
    assert len(frames) == 1


def test_fragmented_message_decodes_frame_by_frame() -> None:
    decoder = FrameDecoder(expect_masked=False, max_frame_bytes=RUNTIME_WS_MAX_FRAME_BYTES)
    first = encode_frame(OP_TEXT, b'{"id"', mask=False, fin=False)
    middle = encode_frame(OP_CONTINUATION, b': 1, "meth', mask=False, fin=False)
    last = encode_frame(OP_CONTINUATION, b'od": "x"}', mask=False, fin=True)
    frames = decoder.feed(first + middle + last)
    assert [frame.opcode for frame in frames] == [OP_TEXT, OP_CONTINUATION, OP_CONTINUATION]
    assert [frame.fin for frame in frames] == [False, False, True]
    assert b"".join(frame.payload for frame in frames) == b'{"id": 1, "method": "x"}'


@pytest.mark.parametrize(
    ("opcode", "payload", "fin", "match"),
    [
        pytest.param(OP_PING, b"p" * 126, True, "125-byte", id="control_payload_over_125"),
        pytest.param(
            OP_CLOSE, CLOSE_NORMAL.to_bytes(2, "big"), False, "fragmented", id="fragmented_control"
        ),
    ],
)
def test_encoder_refuses_malformed_control_frames(
    opcode: int, payload: bytes, fin: bool, match: str
) -> None:
    with pytest.raises(FrameProtocolError, match=match):
        encode_frame(opcode, payload, mask=True, fin=fin)


def _rsv_set() -> bytes:
    encoded = bytearray(encode_frame(OP_TEXT, b"z", mask=False))
    encoded[0] |= 0x40
    return bytes(encoded)


def _unknown_opcode() -> bytes:
    encoded = bytearray(encode_frame(OP_TEXT, b"z", mask=False))
    encoded[0] = (encoded[0] & 0xF0) | 0x3
    return bytes(encoded)


@pytest.mark.parametrize(
    ("frame", "match"),
    [
        pytest.param(_rsv_set(), "rsv", id="rsv_bits"),
        pytest.param(_unknown_opcode(), "unknown websocket opcode", id="unknown_opcode"),
        pytest.param(
            encode_frame(OP_TEXT, b"z", mask=True),
            "server frames must never be masked",
            id="masked_server_frame",
        ),
    ],
)
def test_decoder_rejects_malformed_server_frames(frame: bytes, match: str) -> None:
    with pytest.raises(FrameProtocolError, match=match):
        _decode_single(frame)


def test_unmasked_client_frame_is_rejected_server_side() -> None:
    encoded = encode_frame(OP_TEXT, b"z", mask=False)
    decoder = FrameDecoder(expect_masked=True, max_frame_bytes=RUNTIME_WS_MAX_FRAME_BYTES)
    with pytest.raises(FrameProtocolError, match="client frames must be masked"):
        decoder.feed(encoded)


def test_declared_frame_over_bound_is_rejected_before_buffering() -> None:
    # A declared length beyond the bound, without any payload bytes following.
    header = bytes([0x81, 0x7F]) + (70_000).to_bytes(8, "big")
    decoder = FrameDecoder(expect_masked=False, max_frame_bytes=1024)
    with pytest.raises(FrameProtocolError, match="exceeds the bound"):
        decoder.feed(header)


def test_partial_header_keeps_buffering() -> None:
    decoder = FrameDecoder(expect_masked=False, max_frame_bytes=1024)
    encoded = encode_frame(OP_TEXT, b"payload", mask=False)
    for cut in range(1, len(encoded)):
        decoder = FrameDecoder(expect_masked=False, max_frame_bytes=1024)
        assert decoder.feed(encoded[:cut]) == []
        frames = decoder.feed(encoded[cut:])
        assert len(frames) == 1
        assert frames[0].payload == b"payload"


SOCKET_DIR = Path("/tmp") / "thtr-runtime-ws-tests"


_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


@pytest.fixture
def socket_path(tmp_path: Path) -> Path:
    # tmp_path runs ~120 bytes deep; sun_path caps at 104 on macOS. The pid keeps xdist
    # workers (each with its own counter) off each other's sockets.
    SOCKET_DIR.mkdir(exist_ok=True)
    return SOCKET_DIR / f"ws-{os.getpid()}-{next(_socket_seq)}.sock"


_socket_seq = itertools.count(1)


@pytest.fixture
async def server(socket_path: Path):
    ws_server = WsTestServer(socket_path)
    await ws_server.start()
    yield ws_server
    await ws_server.stop()


@pytest.fixture
async def connection(server: WsTestServer, socket_path: Path):
    io = WebSocketRuntimeIO()
    conn = await io.connect(f"unix://{socket_path}", timeout=5.0)
    await server.wait_handshake()
    yield conn
    await conn.aclose()


# ---- handshake ------------------------------------------------------------


async def test_handshake_sends_upgrade_and_roundtrips_a_request(
    server: WsTestServer, connection
) -> None:
    assert server.request_line.startswith("GET ")
    assert server.request_headers["upgrade"] == "websocket"
    assert server.request_headers["connection"] == "Upgrade"
    assert server.request_headers["sec-websocket-version"] == "13"

    task = asyncio.create_task(connection.request("thread/start", {"prompt": "hi"}, timeout=5.0))
    opcode, payload = await server.next_frame()
    assert opcode == 1
    sent = json.loads(payload)
    assert sent == {"id": 1, "method": "thread/start", "params": {"prompt": "hi"}}
    server.send_json({"id": 1, "result": {"turn": "turn-1"}})
    assert await task == {"turn": "turn-1"}


@pytest.mark.parametrize(
    ("reply", "match"),
    [
        pytest.param(
            b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n",
            "refused the websocket upgrade",
            id="rejected_upgrade",
        ),
        pytest.param(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: c29tZXRoaW5nLWVsc2U=\r\n\r\n",
            "wrong Sec-WebSocket-Accept",
            id="wrong_accept_token",
        ),
        pytest.param(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: h2c\r\n\r\n",
            "did not confirm the websocket upgrade",
            id="upgrade_without_websocket_header",
        ),
        pytest.param(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\nConnection: keep-alive\r\n\r\n",
            r"does not.*include the Upgrade token",
            id="connection_header_without_upgrade_token",
        ),
    ],
)
async def test_invalid_upgrade_reply_raises_typed_handshake_error(
    socket_path: Path, reply: bytes, match: str
) -> None:
    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(reply)
        await writer.drain()
        writer.close()

    server = WsTestServer(socket_path, handler=respond)
    await server.start()
    io = WebSocketRuntimeIO()
    with pytest.raises(RuntimeHandshakeError, match=match):
        await io.connect(f"unix://{socket_path}", timeout=5.0)
    await server.stop()


async def test_handshake_failure_is_not_masked_by_close_errors(
    socket_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transport that errors while closing after a failed handshake must
    never replace the typed handshake failure the caller needs to see."""

    async def exploding_wait_closed(self) -> None:
        raise OSError("connection reset while closing")

    monkeypatch.setattr(asyncio.StreamWriter, "wait_closed", exploding_wait_closed)

    async def refuse(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    server = WsTestServer(socket_path, handler=refuse)
    await server.start()
    io = WebSocketRuntimeIO()
    try:
        with pytest.raises(RuntimeHandshakeError, match="refused the websocket upgrade"):
            await io.connect(f"unix://{socket_path}", timeout=5.0)
    finally:
        monkeypatch.undo()
        await server.stop()


async def test_upgrade_header_tokens_match_case_insensitively(socket_path: Path) -> None:
    async def mixed_case(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        key = ""
        for line in head.decode("latin-1").split("\r\n")[1:]:
            name, separator, value = line.partition(":")
            if separator and name.strip().lower() == "sec-websocket-key":
                key = value.strip()
        accept = base64.b64encode(hashlib.sha1(f"{key}{_WS_GUID}".encode()).digest()).decode()
        writer.write(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: WebSocket\r\n"
                "Connection: keep-alive, UPGRADE\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n"
                "\r\n"
            ).encode("ascii")
        )
        await writer.drain()
        writer.close()

    server = WsTestServer(socket_path, handler=mixed_case)
    await server.start()
    io = WebSocketRuntimeIO()
    conn = await io.connect(f"unix://{socket_path}", timeout=5.0)
    await conn.aclose()
    await server.stop()


async def test_connect_to_missing_endpoint_fails_typed(socket_path: Path) -> None:
    io = WebSocketRuntimeIO()
    with pytest.raises(RuntimeConnectionError, match="cannot reach"):
        await io.connect(f"unix://{socket_path}.missing", timeout=1.0)


async def test_wait_for_endpoint_polls_until_listening(socket_path: Path) -> None:
    await asyncio.wait_for(asyncio.to_thread(_expect_unreachable, f"unix://{socket_path}"), 10.0)


def _expect_unreachable(endpoint: str) -> None:
    import asyncio as aio

    aio.run(_wait_fails(endpoint))


async def _wait_fails(endpoint: str) -> None:
    with pytest.raises(RuntimeConnectionError, match="did not accept"):
        await wait_for_unix_endpoint(endpoint, timeout=0.2)


# ---- correlation ------------------------------------------------------------


async def test_reordered_replies_resolve_their_own_requests(
    server: WsTestServer, connection
) -> None:
    first = asyncio.create_task(connection.request("a", {}, timeout=5.0))
    second = asyncio.create_task(connection.request("b", {}, timeout=5.0))
    await server.drain_frames(2)
    # Reply out of order: id 2 first, then id 1.
    server.send_json({"id": 2, "result": {"which": "second"}})
    server.send_json({"id": 1, "result": {"which": "first"}})
    assert await first == {"which": "first"}
    assert await second == {"which": "second"}


async def test_integer_ids_never_match_string_ids(server: WsTestServer, connection) -> None:
    task = asyncio.create_task(connection.request("a", {}, timeout=0.3))
    await server.drain_frames(1)
    server.send_json({"id": "1", "result": {}})  # string id: a different correlation key
    with pytest.raises(RuntimeRequestTimeout):
        await task
    stats = connection.statistics()
    assert stats.unsolicited_replies == 1


async def test_remote_error_raises_typed_request_error(server: WsTestServer, connection) -> None:
    task = asyncio.create_task(connection.request("boom", {}, timeout=5.0))
    await server.drain_frames(1)
    server.send_json({"id": 1, "error": {"code": -32000, "message": "nope"}})
    with pytest.raises(RuntimeRequestError) as excinfo:
        await task
    assert excinfo.value.code == -32000
    assert excinfo.value.message == "nope"


async def test_deadline_cleanup_leaves_connection_usable(server: WsTestServer, connection) -> None:
    with pytest.raises(RuntimeRequestTimeout, match="no retry, no tmux fallback"):
        await connection.request("slow", {}, timeout=0.2)
    assert connection.statistics().outstanding_requests == 0
    # The late reply must not crash the loop; it is an unsolicited reply.
    server.send_json({"id": 1, "result": {"late": True}})
    await wait_until(lambda: connection.statistics().unsolicited_replies == 1)
    assert connection.statistics().outstanding_requests == 0
    # And the next request still works on the same connection.
    task = asyncio.create_task(connection.request("next", {}, timeout=5.0))
    await server.drain_frames(1)
    server.send_json({"id": 2, "result": {"ok": True}})
    assert await task == {"ok": True}


async def test_outstanding_request_bound_fails_fast(server: WsTestServer, connection) -> None:
    max_outstanding = 16
    tasks = [
        asyncio.create_task(connection.request(f"r{index}", {}, timeout=5.0))
        for index in range(max_outstanding)
    ]
    await server.drain_frames(max_outstanding)
    with pytest.raises(RuntimeConnectionSaturated, match="serialize"):
        await connection.request("one-too-many", {}, timeout=5.0)
    for index, _task in enumerate(tasks):
        server.send_json({"id": index + 1, "result": {"n": index}})
    for index, task in enumerate(tasks):
        assert await task == {"n": index}


async def test_oversized_outgoing_message_is_refused_before_sending(
    server: WsTestServer, socket_path: Path
) -> None:
    io = WebSocketRuntimeIO(max_message_bytes=1024)
    conn = await io.connect(f"unix://{socket_path}", timeout=5.0)
    await server.wait_handshake()
    with pytest.raises(RuntimePayloadTooLarge, match="exceeds the engine bound"):
        await conn.request("big", {"blob": "x" * 2048}, timeout=1.0)
    assert server.frames == []
    await conn.aclose()


# ---- notifications and server requests --------------------------------------


async def test_server_requests_surface_exact_ids_and_are_never_answered(
    server: WsTestServer, connection
) -> None:
    server.send_json({"id": 0, "method": "approval/request", "params": {"cmd": "rm -rf"}})
    server.send_json({"id": "req-9", "method": "approval/request", "params": {}})
    server.send_json({"method": "thread/started", "params": {"threadId": "t1"}})
    iterator = connection.notifications()
    first = await asyncio.wait_for(iterator.__anext__(), 10.0)
    second = await asyncio.wait_for(iterator.__anext__(), 10.0)
    third = await asyncio.wait_for(iterator.__anext__(), 10.0)
    assert first == RuntimeNotification(
        method="approval/request", params={"cmd": "rm -rf"}, request_id=0
    )
    assert isinstance(first.request_id, int)  # exact type preserved: never stringified
    assert second.request_id == "req-9"
    assert isinstance(second.request_id, str)
    assert third.request_id is None
    assert third.method == "thread/started"
    # No reply may ever be sent for a server request; yield and confirm the
    # client wrote nothing new (it has no reply code path at all).
    await asyncio.sleep(0)
    assert server.frames == []


async def test_unrepresentable_server_request_ids_are_skipped_and_counted(
    server: WsTestServer, connection
) -> None:
    server.send_json({"id": True, "method": "approval/request", "params": {}})
    server.send_json({"id": -1, "method": "approval/request", "params": {}})
    server.send_json({"method": "keepalive", "params": {}})
    iterator = connection.notifications()
    notification = await asyncio.wait_for(iterator.__anext__(), 10.0)
    assert notification.method == "keepalive"
    stats = connection.statistics()
    assert stats.skipped_server_requests == 2
    assert stats.buffered_notifications == 0  # the keepalive was consumed, not dropped
    assert server.frames == []


async def test_unknown_notifications_are_delivered_not_dropped(
    server: WsTestServer, connection
) -> None:
    server.send_json({"method": "some/future/method", "params": {"x": 1}})
    iterator = connection.notifications()
    notification = await asyncio.wait_for(iterator.__anext__(), 10.0)
    assert notification.method == "some/future/method"
    assert notification.params == {"x": 1}


async def test_malformed_text_messages_are_counted_and_connection_continues(
    server: WsTestServer, connection
) -> None:
    server.send_frame(1, b"this is not json")
    server.send_json({"method": "still/alive", "params": {}})
    iterator = connection.notifications()
    notification = await asyncio.wait_for(iterator.__anext__(), 10.0)
    assert notification.method == "still/alive"
    assert connection.statistics().malformed_messages == 1


async def test_notify_sends_a_frame_without_an_id(server: WsTestServer, connection) -> None:
    await connection.notify("initialize", {"client": "theater"})
    opcode, payload = await server.next_frame()
    assert opcode == 1
    assert json.loads(payload) == {"method": "initialize", "params": {"client": "theater"}}


# ---- fragmentation -----------------------------------------------------------


async def test_fragmented_reply_assembles_into_one_message(
    server: WsTestServer, connection
) -> None:
    task = asyncio.create_task(connection.request("big/reply", {}, timeout=5.0))
    await server.drain_frames(1)
    server.send_json_fragments({"id": 1, "result": {"value": "ok"}}, [10, 10, 10])
    assert await task == {"value": "ok"}


async def test_fragmented_message_over_the_bound_fails_typed(
    server: WsTestServer, socket_path: Path
) -> None:
    io = WebSocketRuntimeIO(max_message_bytes=64, max_frame_bytes=32)
    conn = await io.connect(f"unix://{socket_path}", timeout=5.0)
    await server.wait_handshake()
    task = asyncio.create_task(conn.request("x", {}, timeout=5.0))
    await server.drain_frames(1)
    server.send_json_fragments({"id": 1, "result": {"value": "v" * 200}}, [32, 32, 32, 32, 32])
    with pytest.raises((RuntimePayloadTooLarge, RuntimeProtocolError)):
        await task
    await conn.aclose()


async def test_oversized_single_frame_message_is_refused(
    server: WsTestServer, socket_path: Path
) -> None:
    # A frame within the frame bound must still respect the message bound:
    # max_frame_bytes > max_message_bytes must not license one huge frame.
    io = WebSocketRuntimeIO(max_frame_bytes=1024, max_message_bytes=64)
    conn = await io.connect(f"unix://{socket_path}", timeout=5.0)
    await server.wait_handshake()
    task = asyncio.create_task(conn.request("x", {}, timeout=5.0))
    await server.drain_frames(1)
    server.send_frame(1, b"x" * 128)
    with pytest.raises(RuntimePayloadTooLarge, match="exceeds the engine bound"):
        await task
    assert conn.statistics().closed is True
    await conn.aclose()


# ---- framing violations -------------------------------------------------------


async def test_oversized_inbound_frame_fails_typed_and_closes(
    server: WsTestServer, socket_path: Path
) -> None:
    io = WebSocketRuntimeIO(max_frame_bytes=1024)
    conn = await io.connect(f"unix://{socket_path}", timeout=5.0)
    await server.wait_handshake()
    task = asyncio.create_task(conn.request("x", {}, timeout=5.0))
    await server.drain_frames(1)
    server.send_frame(1, b"x" * 4096)
    with pytest.raises((RuntimePayloadTooLarge, RuntimeProtocolError)):
        await task
    # The connection is closed with that typed failure: further requests re-raise
    # the same close error, and the iterator ends.
    with pytest.raises((RuntimePayloadTooLarge, RuntimeProtocolError)):
        await conn.request("y", {}, timeout=1.0)
    async for _ in conn.notifications():
        pytest.fail("iterator must end after a framing failure")
    await conn.aclose()


async def test_unexpected_continuation_frame_fails_typed(
    server: WsTestServer, socket_path: Path
) -> None:
    io = WebSocketRuntimeIO()
    conn = await io.connect(f"unix://{socket_path}", timeout=5.0)
    await server.wait_handshake()
    from theater.daemon.harness_runtime.frames import OP_CONTINUATION

    server.send_frame(OP_CONTINUATION, b"orphan", fin=True)
    task = asyncio.create_task(conn.request("x", {}, timeout=5.0))
    await server.drain_frames(1)
    with pytest.raises((RuntimeProtocolError, RuntimeConnectionClosed)):
        await task
    await conn.aclose()


# ---- saturation and overflow ---------------------------------------------------


async def test_notification_overflow_fails_closed_instead_of_dropping_evidence(
    server: WsTestServer, connection
) -> None:
    pending = asyncio.create_task(connection.request("in-flight", {}, timeout=5.0))
    opcode, _payload = await server.next_frame()  # consume the request frame
    assert opcode == 1
    # Fill the buffer to its bound with no consumer, then exceed it.
    for index in range(129):
        server.send_json({"method": "stream/item", "params": {"n": index}})
    await wait_until(lambda: connection.statistics().closed)

    stats = connection.statistics()
    assert stats.closed is True, "buffer saturation must close the connection"
    assert "saturated" in (stats.close_error or "")
    assert "reconciliation" in (stats.close_error or ""), "the reason names the obligation"
    assert stats.dropped_notifications == 1  # the overflowing one, surfaced not silent

    # The in-flight request failed with the same visible typed reason.
    with pytest.raises(RuntimeNotificationOverflow, match="saturated"):
        await pending

    # The client wrote nothing but its request: server requests are never
    # answered, and the overflow added no traffic.
    await asyncio.sleep(0)
    assert server.frames == []


async def test_notification_overflow_reaches_plugin_visible_iteration(
    server: WsTestServer, connection
) -> None:
    """Through the frozen RuntimeConnection surface, an overflow is not a
    silent stream end: the buffered evidence is drained first, then the typed
    RuntimeConnectionError (the frozen vocabulary) is raised to the caller."""
    for index in range(129):
        server.send_json({"method": "stream/item", "params": {"n": index}})
    await wait_until(lambda: connection.statistics().closed)

    # The only surface plugin code holds: RuntimeConnection.notifications().
    iterator = connection.notifications()
    delivered: list[RuntimeNotification] = []
    with pytest.raises(RuntimeConnectionError) as excinfo:
        async for notification in iterator:
            delivered.append(notification)
    # The buffered evidence was drained first; nothing was sacrificed.
    assert [notification.params["n"] for notification in delivered] == list(range(128))
    # The failure is the typed overflow, visible as a frozen-contract error.
    assert isinstance(excinfo.value, RuntimeNotificationOverflow)
    assert "saturated" in str(excinfo.value)
    assert "reconciliation" in str(excinfo.value), "the obligation travels with the raise"


async def test_ordinary_close_still_ends_the_iterator_normally(
    server: WsTestServer, connection
) -> None:
    server.send_json({"method": "stream/item", "params": {"n": 1}})
    await connection.aclose()
    seen: list[RuntimeNotification] = []
    async for notification in connection.notifications():
        seen.append(notification)  # buffered item, then a clean end — no raise
    assert [notification.params["n"] for notification in seen] == [1]


async def test_notification_overflow_after_server_requests_never_answers_them(
    server: WsTestServer, connection
) -> None:
    # Server requests are evidence too: when saturation hits, the connection
    # closes rather than silently discarding them.
    for index in range(129):
        server.send_json({"id": index, "method": "approval/request", "params": {"n": index}})
    await wait_until(lambda: connection.statistics().closed)
    stats = connection.statistics()
    assert stats.closed is True
    assert "saturated" in (stats.close_error or "")
    delivered = 0
    with pytest.raises(RuntimeConnectionError):
        async for _notification in connection.notifications():
            delivered += 1
    assert delivered == 128
    # Nothing was ever answered: no reply frames exist for the server requests.
    await asyncio.sleep(0)
    assert server.frames == []


# ---- ping/pong and close -------------------------------------------------------


async def test_server_ping_gets_an_exact_pong(server: WsTestServer, connection) -> None:
    server.send_frame(9, b"keepalive")
    opcode, payload = await server.next_frame()
    assert (opcode, payload) == (OP_PONG, b"keepalive")


async def test_server_close_ends_pending_requests_and_iterator(
    server: WsTestServer, connection
) -> None:
    task = asyncio.create_task(connection.request("pending", {}, timeout=5.0))
    await server.drain_frames(1)
    server.send_close(1000)
    with pytest.raises(RuntimeConnectionClosed):
        await task
    async for _ in connection.notifications():
        pytest.fail("iterator must end after the backend closes")


async def test_aclose_is_deterministic_and_idempotent(server: WsTestServer, connection) -> None:
    task = asyncio.create_task(connection.request("pending", {}, timeout=5.0))
    opcode, _ = await server.next_frame()  # consume the request frame first
    assert opcode == 1
    await connection.aclose()
    with pytest.raises(RuntimeConnectionClosed):
        await task
    opcode, payload = await server.next_frame()
    assert opcode == OP_CLOSE
    assert int.from_bytes(payload[:2], "big") == 1000
    await connection.aclose()  # idempotent: exactly one close frame, ever
    await wait_until(lambda: not server.frames)
    assert server.frames == []


async def test_backend_disconnect_fails_pending_requests(
    server: WsTestServer, socket_path: Path
) -> None:
    io = WebSocketRuntimeIO()
    conn = await io.connect(f"unix://{socket_path}", timeout=5.0)
    await server.wait_handshake()
    task = asyncio.create_task(conn.request("pending", {}, timeout=5.0))
    await server.drain_frames(1)
    await server.stop()
    with pytest.raises(RuntimeConnectionClosed):
        await task
    await conn.aclose()
