"""Independent byte-level checks for the frozen RC10 public transport."""

from __future__ import annotations

import ast
import contextlib
import json
import shutil
import socket
import tempfile
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from rc10_support.raw_client import (
    MAX_EXACT_JSON_INTEGER,
    FrameDecodeError,
    FrameEOF,
    FrameReader,
    FrameTooLarge,
    HandshakeRequired,
    RawClient,
    ResponseMismatch,
    ResponseShapeError,
    decode_frame,
    encode_frame,
)

FIXTURES = Path(__file__).parent / "fixtures" / "rc10"


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _fixture_frame(name: str) -> bytes:
    return (FIXTURES / name).read_bytes().rstrip(b"\n") + b"\n"


def _read_client_frame(connection: socket.socket) -> bytes:
    data = bytearray()
    while b"\n" not in data:
        chunk = connection.recv(4096)
        if not chunk:
            raise AssertionError("client closed before sending one complete frame")
        data.extend(chunk)
    newline = data.index(b"\n")
    assert newline == len(data) - 1, "the test server expects exactly one request at a time"
    return bytes(data)


def _send_fragments(connection: socket.socket, frame: bytes) -> None:
    offset = 0
    for width in (1, 4, 9):
        connection.sendall(frame[offset : offset + width])
        offset += width
    connection.sendall(frame[offset:])


@contextlib.contextmanager
def _fixture_server(handler: Callable[[socket.socket], None]) -> Iterator[Path]:
    root = Path(tempfile.mkdtemp(prefix="r10raw-", dir="/tmp"))
    path = root / "frontend.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    listener.settimeout(5)
    failures: list[BaseException] = []

    def serve() -> None:
        try:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(5)
                handler(connection)
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        yield path
    finally:
        listener.close()
        thread.join(timeout=5)
        if thread.is_alive():
            raise AssertionError("fixture Unix-socket server did not stop")
        path.unlink(missing_ok=True)
        shutil.rmtree(root)
        if failures:
            raise failures[0]


def test_raw_client_handshake_and_calls_preserve_wire_data() -> None:
    operator = _fixture("operator_handshake_request.json")
    durable = _fixture("durable_mutation_request.json")
    success_frame = _fixture_frame("durable_mutation_success.json")
    refusal_frame = _fixture_frame("durable_mutation_refusal.json")

    def handler(connection: socket.socket) -> None:
        assert _read_client_frame(connection) == _fixture_frame("operator_handshake_request.json")
        _send_fragments(connection, _fixture_frame("operator_handshake_response.json"))
        assert _read_client_frame(connection) == _fixture_frame("durable_mutation_request.json")
        connection.sendall(success_frame + refusal_frame)
        second = _read_client_frame(connection)
        assert b'"idempotency_key"' in second
        assert b'"idempotencyKey"' not in second
        assert json.loads(second) == {
            "id": 3,
            "method": "frontend.controls.send",
            "idempotency_key": "send-a-0008",
            "params": durable["params"],
        }

    with _fixture_server(handler) as socket_path:
        client = RawClient(socket_path)
        assert not client.connected
        with pytest.raises(HandshakeRequired):
            client.call("frontend.contract.get", {})
        client.connect()
        handshake = client.handshake(operator["params"])
        accepted = client.call(
            durable["method"],
            durable["params"],
            idempotency_key=durable["idempotency_key"],
        )
        refused = client.call(
            durable["method"],
            durable["params"],
            idempotency_key="send-a-0008",
        )
        client.close()

    assert handshake["future_envelope_field"] == "kept"
    assert handshake["result"]["future_handshake_value"] == {"revision": 2}
    assert accepted["future_envelope_field"] == {"revision": 3}
    assert accepted["result"]["future_operation_value"] == "kept"
    assert refused["error"]["code"] == "future_provider_refusal"
    assert refused["error"]["future_error_field"] == ["kept"]
    assert refused["future_envelope_field"] is True


def test_raw_decoder_preserves_provider_callback_and_future_event_values() -> None:
    left, right = socket.socketpair()
    try:
        right.sendall(
            _fixture_frame("callback_request.json") + _fixture_frame("callback_response.json")
        )
        reader = FrameReader()
        callback_request = reader.read(left)
        callback_response = reader.read(left)
    finally:
        left.close()
        right.close()

    transaction = decode_frame(_fixture_frame("event_transaction_future.json"))
    provider_handshake = decode_frame(_fixture_frame("provider_handshake_request.json"))
    provider_response = decode_frame(_fixture_frame("provider_handshake_response.json"))
    future_response = decode_frame(_fixture_frame("future_enum_response.json"))
    assert callback_request["method"] == "terminal.deliver"
    assert callback_request["params"]["terminal_incarnation"] == "incarnation-a"
    assert callback_response["result"]["delivery"] == "accepted"
    assert provider_handshake["params"]["channel"] == "callback"
    assert provider_response["result"]["provider_generation"] == 4
    assert future_response["result"]["state"] == "paused-by-provider"
    assert transaction["events"][0]["kind"] == "future.entity_changed"
    assert transaction["events"][0]["future_event_field"] == "kept"
    assert transaction["future_transaction_field"] == {"kept": True}


@pytest.mark.parametrize(
    ("payload", "limit", "error"),
    [
        (b'{"id":1,}\n', 64, FrameDecodeError),
        (b"[]\n", 64, FrameDecodeError),
        (b'{"id":1,"ok":true,"result":NaN}\n', 64, FrameDecodeError),
        (b'{"id":1,"ok":true,"result":1e9999}\n', 64, FrameDecodeError),
        (b'{"id":1', 64, FrameEOF),
        (b'{"too_long":"0123456789"}\n', 16, FrameTooLarge),
    ],
)
def test_frame_reader_rejects_malformed_eof_and_oversized_frames(
    payload: bytes, limit: int, error: type[Exception]
) -> None:
    left, right = socket.socketpair()
    try:
        right.sendall(payload)
        right.shutdown(socket.SHUT_WR)
        with pytest.raises(error):
            FrameReader(limit=limit).read(left)
    finally:
        left.close()
        right.close()


def test_frame_codec_enforces_outbound_ceiling_and_newline() -> None:
    with pytest.raises(FrameTooLarge):
        encode_frame({"payload": "0123456789"}, limit=16)
    with pytest.raises(FrameDecodeError, match="newline"):
        decode_frame(b'{"id":1}')


def test_raw_client_accepts_refusal_without_details() -> None:
    operator = _fixture("operator_handshake_request.json")
    refusal = {
        "id": 1,
        "ok": False,
        "error": {
            "code": "future_refusal",
            "message": "A newer provider rejected this request.",
            "future_error_field": {"kept": True},
        },
        "future_envelope_field": ["kept"],
    }

    def handler(connection: socket.socket) -> None:
        _read_client_frame(connection)
        connection.sendall(encode_frame(refusal))

    with _fixture_server(handler) as socket_path:
        client = RawClient(socket_path)
        client.connect()
        response = client.handshake(operator["params"])
        client.close()

    assert "details" not in response["error"]
    assert response["error"]["future_error_field"] == {"kept": True}
    assert response["future_envelope_field"] == ["kept"]


@pytest.mark.parametrize(
    ("response", "error"),
    [
        ({"id": 2, "ok": True, "result": {}}, ResponseMismatch),
        ({"id": 0, "ok": True, "result": {}}, ResponseShapeError),
        (
            {"id": 0, "ok": False, "error": {"code": "uncorrelated", "message": "nope"}},
            ResponseMismatch,
        ),
        ({"id": True, "ok": True, "result": {}}, ResponseShapeError),
        ({"id": MAX_EXACT_JSON_INTEGER + 1, "ok": True, "result": {}}, ResponseShapeError),
        ({"id": 1, "ok": True}, ResponseShapeError),
        (
            {
                "id": 1,
                "ok": False,
                "error": {"code": "future", "message": "bad details", "details": []},
            },
            ResponseShapeError,
        ),
        (
            {
                "id": 1,
                "ok": False,
                "result": {},
                "error": {"code": "future", "message": "both", "details": {}},
            },
            ResponseShapeError,
        ),
    ],
)
def test_raw_client_rejects_invalid_or_mismatched_responses(
    response: dict[str, object], error: type[Exception]
) -> None:
    operator = _fixture("operator_handshake_request.json")

    def handler(connection: socket.socket) -> None:
        _read_client_frame(connection)
        connection.sendall(encode_frame(response))

    with _fixture_server(handler) as socket_path:
        client = RawClient(socket_path)
        client.connect()
        with pytest.raises(error):
            client.handshake(operator["params"])
        assert not client.connected
        with pytest.raises(HandshakeRequired):
            client.call("frontend.contract.get", {})


def test_raw_client_uses_only_stdlib_imports() -> None:
    path = Path(__file__).parent / "rc10_support" / "raw_client.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.add(node.module)
    assert not {name for name in imports if name.startswith(("theater", "jsonschema"))}
