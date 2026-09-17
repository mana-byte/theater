"""Minimal stdlib-only RC10 frontend NDJSON client for contract tests."""

from __future__ import annotations

import contextlib
import json
import math
import socket
from collections.abc import Mapping
from pathlib import Path
from typing import Final, cast

MAX_FRAME_BYTES: Final = 64 * 1024 * 1024
MAX_EXACT_JSON_INTEGER: Final = 9_007_199_254_740_991
_RECEIVE_CHUNK_BYTES: Final = 64 * 1024


class RawClientError(RuntimeError):
    """Base error for the independent raw contract consumer."""


class ClientStateError(RawClientError):
    """The client lifecycle does not permit the requested action."""


class HandshakeRequired(ClientStateError):
    """An ordinary request was attempted before a successful handshake."""


class FrameError(RawClientError):
    """A peer frame could not be safely decoded."""


class FrameTooLarge(FrameError):
    """A serialized NDJSON frame exceeds the configured absolute ceiling."""


class FrameEOF(FrameError):
    """The peer closed before supplying one complete newline-terminated frame."""


class FrameDecodeError(FrameError):
    """A complete frame was not valid finite JSON object data."""


class ResponseMismatch(RawClientError):
    """A response did not correlate with the one request in flight."""


class ResponseShapeError(RawClientError):
    """A correlated response omitted or contradicted required envelope fields."""


def encode_frame(value: Mapping[str, object], *, limit: int = MAX_FRAME_BYTES) -> bytes:
    """Serialize one finite JSON object as a bounded NDJSON frame."""
    _validate_limit(limit)
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise FrameDecodeError("outbound frame must be an object with string keys")
    try:
        body = json.dumps(dict(value), allow_nan=False, ensure_ascii=False, separators=(",", ":"))
        frame = body.encode("utf-8") + b"\n"
    except (TypeError, UnicodeError, ValueError) as exc:
        raise FrameDecodeError(f"outbound frame is not finite JSON: {exc}") from exc
    if len(frame) > limit:
        raise FrameTooLarge(f"outbound frame is {len(frame)} bytes, over the {limit}-byte ceiling")
    return frame


def decode_frame(frame: bytes, *, limit: int = MAX_FRAME_BYTES) -> dict[str, object]:
    """Decode exactly one bounded newline-terminated JSON object frame."""
    _validate_limit(limit)
    if len(frame) > limit:
        raise FrameTooLarge(f"inbound frame is {len(frame)} bytes, over the {limit}-byte ceiling")
    if not frame.endswith(b"\n"):
        raise FrameDecodeError("inbound frame is not newline-terminated")
    if b"\n" in frame[:-1]:
        raise FrameDecodeError("inbound frame contains more than one NDJSON line")
    try:
        value = json.loads(
            frame[:-1].decode("utf-8"),
            parse_constant=_reject_nonfinite,
            parse_float=_parse_finite_float,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise FrameDecodeError(f"inbound frame is not finite JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise FrameDecodeError("inbound frame must contain one JSON object")
    return cast(dict[str, object], value)


class FrameReader:
    """Incrementally reads bounded NDJSON objects while retaining later complete frames."""

    def __init__(self, *, limit: int = MAX_FRAME_BYTES) -> None:
        _validate_limit(limit)
        self._limit = limit
        self._buffer = bytearray()

    def read(self, connection: socket.socket) -> dict[str, object]:
        """Read one frame from a stream that may split or coalesce writes."""
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                frame_length = newline + 1
                if frame_length > self._limit:
                    raise FrameTooLarge(
                        f"inbound frame is {frame_length} bytes, over the "
                        f"{self._limit}-byte ceiling"
                    )
                frame = bytes(self._buffer[:frame_length])
                del self._buffer[:frame_length]
                return decode_frame(frame, limit=self._limit)
            if len(self._buffer) >= self._limit:
                raise FrameTooLarge(
                    f"inbound frame exceeds the {self._limit}-byte ceiling before its newline"
                )
            chunk = connection.recv(min(_RECEIVE_CHUNK_BYTES, self._limit - len(self._buffer) + 1))
            if not chunk:
                raise FrameEOF("peer closed before sending a complete newline-terminated frame")
            self._buffer.extend(chunk)


class RawClient:
    """One synchronous public connection with no daemon startup, retries, or replay."""

    def __init__(
        self,
        socket_path: str | Path,
        *,
        limit: int = MAX_FRAME_BYTES,
        first_request_id: int = 1,
    ) -> None:
        _validate_limit(limit)
        _validate_request_id(first_request_id, "first_request_id")
        self._socket_path = str(socket_path)
        self._reader = FrameReader(limit=limit)
        self._limit = limit
        self._next_request_id = first_request_id
        self._connection: socket.socket | None = None
        self._state = "new"
        self._in_flight: int | None = None

    @property
    def connected(self) -> bool:
        return self._connection is not None

    def connect(self) -> None:
        """Connect to the supplied Unix socket without starting any process."""
        if self._state != "new" or self._connection is not None:
            raise ClientStateError("raw client can only connect once")
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.connect(self._socket_path)
        except OSError:
            connection.close()
            raise
        self._connection = connection

    def close(self) -> None:
        """Close the explicitly connected socket; accepted work is never replayed."""
        connection, self._connection = self._connection, None
        if connection is not None:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            connection.close()
        self._state = "closed"
        self._in_flight = None

    def handshake(
        self,
        params: Mapping[str, object],
        *,
        meta: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Send the mandatory first frontend.handshake frame."""
        if self._connection is None:
            raise ClientStateError("raw client must connect before its handshake")
        if self._state != "new":
            raise ClientStateError("frontend.handshake is only allowed as the first request")
        response = self._round_trip("frontend.handshake", params, meta=meta)
        self._state = "ready" if response["ok"] is True else "failed"
        return response

    def call(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
        meta: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Send one ordinary request after a successful handshake."""
        if self._state != "ready":
            raise HandshakeRequired("frontend.handshake must succeed before ordinary requests")
        if method == "frontend.handshake":
            raise ClientStateError("use handshake() for frontend.handshake")
        return self._round_trip(method, params, idempotency_key=idempotency_key, meta=meta)

    def _round_trip(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
        meta: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        connection = self._connection
        if connection is None:
            raise ClientStateError("raw client is not connected")
        if self._in_flight is not None:
            raise ClientStateError("only one ordinary request may be in flight")
        if not isinstance(method, str) or not method:
            raise TypeError("method must be a non-empty string")
        request = {"id": self._allocate_id(), "method": method}
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str) or not idempotency_key:
                raise TypeError("idempotency_key must be a non-empty string")
            request["idempotency_key"] = idempotency_key
        request["params"] = _copy_object(params, "params")
        if meta is not None:
            request["_meta"] = _copy_object(meta, "_meta")
        request_id = cast(int, request["id"])
        self._in_flight = request_id
        try:
            connection.sendall(encode_frame(request, limit=self._limit))
            response = self._reader.read(connection)
            _validate_response(response, request_id)
        except (OSError, RawClientError):
            self._poison()
            raise
        else:
            return response
        finally:
            self._in_flight = None

    def _allocate_id(self) -> int:
        request_id = self._next_request_id
        _validate_request_id(request_id, "request id")
        if request_id == MAX_EXACT_JSON_INTEGER:
            self._next_request_id += 1
        else:
            self._next_request_id = request_id + 1
        return request_id

    def _poison(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            with contextlib.suppress(OSError):
                connection.close()
        self._state = "broken"


def _copy_object(value: Mapping[str, object], label: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise TypeError(f"{label} must be an object with string keys")
    return dict(value)


def _validate_response(response: Mapping[str, object], request_id: int) -> None:
    response_id = response.get("id")
    _validate_request_id(response_id, "response.id")
    if response_id != request_id:
        raise ResponseMismatch(f"received response id {response_id}, expected {request_id}")
    ok = response.get("ok")
    if type(ok) is not bool:
        raise ResponseShapeError("response.ok must be a boolean")
    has_result = "result" in response
    has_error = "error" in response
    if ok is True:
        if not has_result or has_error:
            raise ResponseShapeError("successful response must contain result and no error")
        return
    if not has_error or has_result:
        raise ResponseShapeError("refusal response must contain error and no result")
    error = response["error"]
    if not isinstance(error, Mapping):
        raise ResponseShapeError("response.error must be an object")
    code = error.get("code")
    message = error.get("message")
    details = error.get("details")
    if not isinstance(code, str) or not code:
        raise ResponseShapeError("response.error.code must be a non-empty string")
    if not isinstance(message, str):
        raise ResponseShapeError("response.error.message must be a string")
    if not isinstance(details, Mapping):
        raise ResponseShapeError("response.error.details must be an object")


def _validate_request_id(value: object, label: str) -> None:
    if type(value) is not int or not 1 <= value <= MAX_EXACT_JSON_INTEGER:
        raise ResponseShapeError(
            f"{label} must be a positive exact JSON integer no greater than "
            f"{MAX_EXACT_JSON_INTEGER}"
        )


def _validate_limit(limit: int) -> None:
    if type(limit) is not int or limit < 1:
        raise ValueError("frame limit must be a positive integer")


def _reject_nonfinite(value: str) -> object:
    raise ValueError(f"non-finite JSON value {value!r} is not allowed")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON value {value!r} is not allowed")
    return parsed
