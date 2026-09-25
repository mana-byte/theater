"""Pure RFC 6455 frame codec for the shared runtime WebSocket transport.

No I/O, so every framing rule (masking direction, fragmentation, control bounds, RSV
bits, payload caps checked before buffering) is testable against crafted bytes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

#: The single close status code this engine sends or accepts as a reply.
CLOSE_NORMAL = 1000

_CONTROL_OPCODES = frozenset({OP_CLOSE, OP_PING, OP_PONG})
_DATA_OPCODES = frozenset({OP_CONTINUATION, OP_TEXT, OP_BINARY})
_KNOWN_OPCODES = _CONTROL_OPCODES | _DATA_OPCODES


class FrameProtocolError(ValueError):
    """One WebSocket framing violation."""


@dataclass(frozen=True, slots=True)
class DecodedFrame:
    """One fully received frame; fragmented data frames arrive per-chunk."""

    opcode: int
    payload: bytes
    fin: bool
    masked: bool


def _mask_bytes(payload: bytes, key: bytes) -> bytes:
    if not payload:
        return b""
    repeats = -(-len(payload) // 4)
    keystream = (key * repeats)[: len(payload)]
    masked = int.from_bytes(payload, "big") ^ int.from_bytes(keystream, "big")
    return masked.to_bytes(len(payload), "big")


def _validate_control_frame(fin: bool, length: int) -> None:
    if not fin:
        raise FrameProtocolError("control frames must not be fragmented")
    if length > 125:
        raise FrameProtocolError("control frame payload exceeds the 125-byte limit")


def encode_frame(
    opcode: int,
    payload: bytes | bytearray,
    *,
    mask: bool,
    fin: bool = True,
) -> bytes:
    """Encode one frame, optionally masked with a fresh random key.

    Refuses fragmented or oversized control frames (RFC 6455 §5.5).
    """
    if opcode not in _KNOWN_OPCODES:
        raise FrameProtocolError(f"unknown websocket opcode {opcode:#x}")
    if opcode in _CONTROL_OPCODES:
        _validate_control_frame(fin, len(payload))
    header = bytearray()
    header.append((0x80 if fin else 0x00) | opcode)
    length = len(payload)
    mask_bit = 0x80 if mask else 0x00
    if length < 126:
        header.append(mask_bit | length)
    elif length <= 0xFFFF:
        header.append(mask_bit | 126)
        header += length.to_bytes(2, "big")
    else:
        header.append(mask_bit | 127)
        header += length.to_bytes(8, "big")
    if mask:
        key = os.urandom(4)
        header += key
        return bytes(header) + _mask_bytes(bytes(payload), key)
    return bytes(header) + bytes(payload)


class FrameDecoder:
    """Incremental frame decoder fed with raw socket bytes.

    ``expect_masked`` pins the masking direction: clients get unmasked server frames and
    servers get masked client frames.
    """

    def __init__(self, *, expect_masked: bool, max_frame_bytes: int) -> None:
        self._expect_masked = expect_masked
        self._max_frame_bytes = max_frame_bytes
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[DecodedFrame]:
        self._buffer += data
        frames: list[DecodedFrame] = []
        while True:
            frame = self._decode_one()
            if frame is None:
                return frames
            frames.append(frame)

    def _decode_one(self) -> DecodedFrame | None:
        buffer = self._buffer
        if len(buffer) < 2:
            return None
        first, second = buffer[0], buffer[1]
        fin = bool(first & 0x80)
        if first & 0x70:
            raise FrameProtocolError("rsv bits set without a negotiated extension")
        opcode = first & 0x0F
        if opcode not in _KNOWN_OPCODES:
            raise FrameProtocolError(f"unknown websocket opcode {opcode:#x}")
        masked = bool(second & 0x80)
        if masked != self._expect_masked:
            if self._expect_masked:
                raise FrameProtocolError("client frames must be masked")
            raise FrameProtocolError("server frames must never be masked")
        parsed = self._parse_length(second & 0x7F)
        if parsed is None:
            return None
        length, offset = parsed
        if length > self._max_frame_bytes:
            raise FrameProtocolError(
                f"frame payload of {length} bytes exceeds the bound {self._max_frame_bytes}"
            )
        if opcode in _CONTROL_OPCODES:
            _validate_control_frame(fin, length)
        mask = b""
        if masked:
            if len(buffer) < offset + 4:
                return None
            mask = bytes(buffer[offset : offset + 4])
            offset += 4
        if len(buffer) < offset + length:
            return None
        payload = bytes(buffer[offset : offset + length])
        del self._buffer[: offset + length]
        if masked:
            payload = _mask_bytes(payload, mask)
        return DecodedFrame(opcode=opcode, payload=payload, fin=fin, masked=masked)

    def _parse_length(self, short_length: int) -> tuple[int, int] | None:
        """Resolve the extended payload length, or None while bytes are missing."""
        buffer = self._buffer
        if short_length < 126:
            return short_length, 2
        if short_length == 126:
            if len(buffer) < 4:
                return None
            return int.from_bytes(buffer[2:4], "big"), 4
        if len(buffer) < 10:
            return None
        return int.from_bytes(buffer[2:10], "big"), 10


__all__ = [
    "CLOSE_NORMAL",
    "OP_BINARY",
    "OP_CLOSE",
    "OP_CONTINUATION",
    "OP_PING",
    "OP_PONG",
    "OP_TEXT",
    "DecodedFrame",
    "FrameDecoder",
    "FrameProtocolError",
    "encode_frame",
]
