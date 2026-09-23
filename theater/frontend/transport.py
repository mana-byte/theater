"""Bounded connect-only NDJSON transport for public frontend client lanes."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Final, cast

from theater.frontend.capabilities import MAX_EXACT_JSON_INTEGER, MAX_FRAME_BYTES

_RECEIVE_CHUNK_BYTES: Final = 64 * 1024


class FrontendTransportError(RuntimeError):
    """Base error for public frontend socket transport."""


class TransportStateError(FrontendTransportError):
    """The transport lifecycle does not permit the requested action."""


class TransportConnectionError(FrontendTransportError):
    """The explicit Unix-socket connection could not be established or retained."""


class TransportProtocolError(FrontendTransportError):
    """The peer sent a malformed, oversized, or incomplete frame."""


class TransportBusy(TransportStateError):
    """One ordinary request is already in flight on this connection."""


class RequestUncertain(TransportConnectionError):
    """A connection failed after submission, so the request must not be replayed."""

    def __init__(self, method: str, request_id: int) -> None:
        super().__init__(
            f"connection lost after submitting {method} with request id {request_id}; "
            "the daemon may have accepted it, so the SDK will not retry"
        )
        self.method = method
        self.request_id = request_id


class _FrameError(ValueError):
    """A frame cannot safely be encoded or decoded."""


class _FrameTooLarge(_FrameError):
    """A frame exceeded the absolute negotiated ceiling."""


class _FrameEOF(_FrameError):
    """The peer closed without completing its one response frame."""


class _FrameReader:
    """Incrementally read one bounded NDJSON object while retaining later frames."""

    def __init__(self, *, limit: int) -> None:
        _validate_limit(limit)
        self._limit = limit
        self._buffer = bytearray()

    def set_limit(self, limit: int) -> None:
        _validate_limit(limit)
        self._limit = limit

    async def read(self, reader: asyncio.StreamReader) -> dict[str, object]:
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                frame_length = newline + 1
                if frame_length > self._limit:
                    raise _FrameTooLarge(
                        f"inbound frame is {frame_length} bytes, over the "
                        f"{self._limit}-byte ceiling"
                    )
                frame = bytes(self._buffer[:frame_length])
                del self._buffer[:frame_length]
                return _decode_frame(frame, limit=self._limit)
            if len(self._buffer) >= self._limit:
                raise _FrameTooLarge(
                    f"inbound frame exceeds the {self._limit}-byte ceiling before its newline"
                )
            chunk = await reader.read(
                min(_RECEIVE_CHUNK_BYTES, self._limit - len(self._buffer) + 1)
            )
            if not chunk:
                raise _FrameEOF("peer closed before sending a complete newline-terminated frame")
            self._buffer.extend(chunk)


class FrontendTransport:
    """One internal ordinary connection; callers own handshake and request validation."""

    def __init__(self, socket_path: str | Path, *, limit: int = MAX_FRAME_BYTES) -> None:
        _validate_limit(limit)
        self._socket_path = str(socket_path)
        self._limit = limit
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._frame_reader = _FrameReader(limit=limit)
        self._in_flight: int | None = None
        self._closed = False

    @property
    def connected(self) -> bool:
        """Open, and not already hung up by the daemon (e.g. after a restart)."""
        reader, writer = self._reader, self._writer
        return (
            reader is not None
            and writer is not None
            and not reader.at_eof()
            and not writer.is_closing()
        )

    @property
    def in_flight_id(self) -> int | None:
        return self._in_flight

    async def connect(self) -> None:
        """Connect only to the supplied socket path; this never starts a process."""
        if self._closed:
            raise TransportStateError("closed frontend transport cannot reconnect")
        if self.connected:
            return
        try:
            reader, writer = await asyncio.open_unix_connection(
                self._socket_path, limit=self._limit
            )
        except OSError as exc:
            raise TransportConnectionError(
                f"could not connect to the explicit frontend socket {self._socket_path}: {exc}"
            ) from exc
        self._reader = reader
        self._writer = writer

    def set_frame_limit(self, limit: int) -> None:
        """Apply a valid negotiated ceiling before the next frame is sent or read."""
        _validate_limit(limit)
        if limit > MAX_FRAME_BYTES:
            raise TransportProtocolError(
                f"negotiated frame limit {limit} exceeds the {MAX_FRAME_BYTES}-byte public ceiling"
            )
        self._limit = limit
        self._frame_reader.set_limit(limit)

    async def close(self) -> None:
        """Close this lane without affecting any other client connection."""
        writer = self._writer
        self._reader = None
        self._writer = None
        self._in_flight = None
        self._closed = True
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError, ConnectionError):
                await writer.wait_closed()

    def abort(self) -> None:
        """Synchronously retire this lane after cancellation or protocol loss."""
        writer = self._writer
        self._reader = None
        self._writer = None
        self._in_flight = None
        self._closed = True
        if writer is not None:
            writer.close()

    async def _exchange(self, request: Mapping[str, object]) -> dict[str, object]:
        """Write one already-validated request and read exactly one correlated frame."""
        reader = self._reader
        writer = self._writer
        if reader is None or writer is None:
            raise TransportStateError("frontend transport is not connected")
        if self._in_flight is not None:
            raise TransportBusy("only one ordinary request may be in flight per connection")
        request_id = request.get("id")
        method = request.get("method")
        if type(request_id) is not int or not isinstance(method, str):
            raise TransportStateError("frontend client must supply an integer id and method")
        try:
            frame = _encode_frame(request, limit=self._limit)
        except _FrameError as exc:
            raise TransportProtocolError(str(exc)) from exc

        self._in_flight = request_id
        dispatched = False
        try:
            writer.write(frame)
            dispatched = True
            await writer.drain()
            return await self._frame_reader.read(reader)
        except asyncio.CancelledError:
            self.abort()
            raise
        except _FrameEOF as exc:
            self.abort()
            if dispatched:
                raise RequestUncertain(method, request_id) from exc
            raise TransportConnectionError(
                f"connection closed before sending {method}: {exc}"
            ) from exc
        except _FrameError as exc:
            self.abort()
            raise TransportProtocolError(str(exc)) from exc
        except (OSError, ConnectionError) as exc:
            self.abort()
            if dispatched:
                raise RequestUncertain(method, request_id) from exc
            raise TransportConnectionError(
                f"connection failed before sending {method}: {exc}"
            ) from exc
        finally:
            self._in_flight = None


def _encode_frame(value: Mapping[str, object], *, limit: int) -> bytes:
    if any(type(key) is not str for key in value):
        raise _FrameError("outbound frame must be an object with string keys")
    try:
        encoded = json.dumps(
            dict(value), allow_nan=False, ensure_ascii=False, separators=(",", ":")
        )
        frame = encoded.encode("utf-8") + b"\n"
    except (TypeError, UnicodeError, ValueError) as exc:
        raise _FrameError(f"outbound frame is not finite JSON: {exc}") from exc
    if len(frame) > limit:
        raise _FrameTooLarge(f"outbound frame is {len(frame)} bytes, over the {limit}-byte ceiling")
    return frame


def _decode_frame(frame: bytes, *, limit: int) -> dict[str, object]:
    if len(frame) > limit:
        raise _FrameTooLarge(f"inbound frame is {len(frame)} bytes, over the {limit}-byte ceiling")
    if not frame.endswith(b"\n"):
        raise _FrameError("inbound frame is not newline-terminated")
    if b"\n" in frame[:-1]:
        raise _FrameError("inbound frame contains more than one NDJSON line")
    try:
        value = json.loads(
            frame[:-1].decode("utf-8"),
            parse_constant=_reject_nonfinite,
            parse_float=_parse_finite_float,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise _FrameError(f"inbound frame is not finite JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise _FrameError("inbound frame must contain one JSON object")
    return cast(dict[str, object], value)


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON value {value!r} is not allowed")
    return parsed


def _reject_nonfinite(value: str) -> object:
    raise ValueError(f"non-finite JSON value {value!r} is not allowed")


def _validate_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= MAX_FRAME_BYTES:
        raise ValueError(f"frame limit must be an integer from 1 through {MAX_FRAME_BYTES}")


def valid_request_id(value: object) -> bool:
    """Return whether a value is a positive interoperable JSON request id."""
    return type(value) is int and 1 <= value <= MAX_EXACT_JSON_INTEGER


__all__ = [
    "FrontendTransport",
    "FrontendTransportError",
    "RequestUncertain",
    "TransportBusy",
    "TransportConnectionError",
    "TransportProtocolError",
    "TransportStateError",
    "valid_request_id",
]
