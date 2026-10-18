"""Focused tests for the pure RFC 6455 frame codec behind the runtime transport."""

from __future__ import annotations

import pytest

from theater.daemon.harness_runtime.constants import RUNTIME_WS_MAX_FRAME_BYTES
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


def test_unmasked_roundtrip() -> None:
    encoded = encode_frame(OP_TEXT, b"hello", mask=False)
    opcode, payload, _ = _decode_single(encoded)
    assert (opcode, payload) == (OP_TEXT, b"hello")


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


def test_sixteen_bit_extended_length() -> None:
    payload = bytes(range(256)) * 3  # 768 bytes -> 16-bit length
    encoded = encode_frame(OP_TEXT, payload, mask=False)
    opcode, decoded, _ = _decode_single(encoded)
    assert (opcode, decoded) == (OP_TEXT, payload)


def test_sixty_four_bit_extended_length() -> None:
    payload = b"y" * 70_000
    encoded = encode_frame(OP_TEXT, payload, mask=False)
    opcode, decoded, _ = _decode_single(encoded)
    assert (opcode, decoded) == (OP_TEXT, payload)


def test_empty_payload_roundtrip() -> None:
    encoded = encode_frame(OP_PONG, b"", mask=False)
    opcode, payload, _ = _decode_single(encoded)
    assert (opcode, payload) == (OP_PONG, b"")


def test_fragmented_message_decodes_frame_by_frame() -> None:
    decoder = FrameDecoder(expect_masked=False, max_frame_bytes=RUNTIME_WS_MAX_FRAME_BYTES)
    first = encode_frame(OP_TEXT, b'{"id"', mask=False, fin=False)
    middle = encode_frame(OP_CONTINUATION, b': 1, "meth', mask=False, fin=False)
    last = encode_frame(OP_CONTINUATION, b'od": "x"}', mask=False, fin=True)
    frames = decoder.feed(first + middle + last)
    assert [frame.opcode for frame in frames] == [OP_TEXT, OP_CONTINUATION, OP_CONTINUATION]
    assert [frame.fin for frame in frames] == [False, False, True]
    assert b"".join(frame.payload for frame in frames) == b'{"id": 1, "method": "x"}'


def test_control_payload_over_125_is_refused_by_encoder() -> None:
    with pytest.raises(FrameProtocolError, match="125-byte"):
        encode_frame(OP_PING, b"p" * 126, mask=True)


def test_fragmented_control_frame_is_refused_by_encoder() -> None:
    with pytest.raises(FrameProtocolError, match="fragmented"):
        encode_frame(OP_CLOSE, CLOSE_NORMAL.to_bytes(2, "big"), mask=True, fin=False)


def test_rsv_bits_are_rejected() -> None:
    encoded = bytearray(encode_frame(OP_TEXT, b"z", mask=False))
    encoded[0] |= 0x40
    with pytest.raises(FrameProtocolError, match="rsv"):
        _decode_single(bytes(encoded))


def test_unknown_opcode_is_rejected() -> None:
    encoded = bytearray(encode_frame(OP_TEXT, b"z", mask=False))
    encoded[0] = (encoded[0] & 0xF0) | 0x3
    with pytest.raises(FrameProtocolError, match="unknown websocket opcode"):
        _decode_single(bytes(encoded))


def test_masked_server_frame_is_rejected() -> None:
    encoded = encode_frame(OP_TEXT, b"z", mask=True)
    with pytest.raises(FrameProtocolError, match="server frames must never be masked"):
        _decode_single(encoded)


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
