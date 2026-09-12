"""Bounded request correlation for the authenticated frontend connection."""

from __future__ import annotations

import asyncio
import json
import math
import uuid
from collections.abc import Awaitable, Callable, Mapping

from theater.daemon.harness_runtime.errors import (
    RuntimeConnectionSaturated,
    RuntimeMalformedReply,
    RuntimePayloadTooLarge,
)
from theater.harness.contracts.runtime import (
    RuntimeConnectionClosed,
    RuntimeRequestError,
    RuntimeRequestTimeout,
)

FRONTEND_REQUEST_MAX_PENDING = 32
FRONTEND_REQUEST_MAX_BYTES = 65536
FRONTEND_REQUEST_ID_MAX_CHARS = 512


def _json_default(value):
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("frontend requests must contain JSON values")


class FrontendRequests:
    """One connection's request ids; reconnect creates a fresh instance.

    The host authenticates the connection and supplies the bounded writer.
    Requests are sent once. Late replies may be ignored after timeout, but a
    possibly applied mutation is never replayed or moved to another peer.
    """

    def __init__(self, send_frame: Callable[[bytes], Awaitable[None]]) -> None:
        self._send_frame = send_frame
        self._pending: dict[str, asyncio.Future[Mapping[str, object]]] = {}
        self._closed = False

    async def request(
        self, method: str, params: Mapping[str, object], *, timeout: float
    ) -> Mapping[str, object]:
        if self._closed:
            raise RuntimeConnectionClosed("frontend connection is closed")
        if not isinstance(method, str) or not method or len(method) > 512:
            raise ValueError("frontend request method must be a bounded non-blank string")
        if not isinstance(params, Mapping):
            raise TypeError("frontend request parameters must be an object")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("frontend request timeout must be finite and positive")
        if len(self._pending) >= FRONTEND_REQUEST_MAX_PENDING:
            raise RuntimeConnectionSaturated("frontend pending request limit reached")
        request_id = uuid.uuid4().hex
        payload = (
            json.dumps(
                {"type": "request", "id": request_id, "method": method, "params": params},
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
                default=_json_default,
            ).encode("utf-8")
            + b"\n"
        )
        if len(payload) > FRONTEND_REQUEST_MAX_BYTES:
            raise RuntimePayloadTooLarge("frontend request exceeds the frame size limit")
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            async with asyncio.timeout(timeout):
                await self._send_frame(payload)
                return await future
        except TimeoutError as exc:
            raise RuntimeRequestTimeout(
                "frontend request timed out; delivery may have occurred"
            ) from exc
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                # A close can fail the future while its writer is still
                # awaiting backpressure. Consume that exception on timeout or
                # cancellation too, so it cannot become an unhandled future.
                future.exception()

    def receive(self, frame: Mapping[str, object]) -> bool:
        """Accept a matching response, or ignore an expired/duplicate id."""
        request_id = frame.get("id")
        if (
            frame.get("type") != "response"
            or not isinstance(request_id, str)
            or not request_id
            or len(request_id) > FRONTEND_REQUEST_ID_MAX_CHARS
            or ("result" in frame) == ("error" in frame)
        ):
            raise RuntimeMalformedReply("frontend response has an invalid envelope")
        future = self._pending.get(request_id)
        if future is None or future.done():
            return False
        if "error" in frame:
            error = frame["error"]
            if (
                not isinstance(error, Mapping)
                or type(error.get("code")) not in (str, int)
                or not isinstance(error.get("message"), str)
            ):
                raise RuntimeMalformedReply("frontend response error is malformed")
            message = error["message"]
            assert isinstance(message, str)
            future.set_exception(RuntimeRequestError(error["code"], message[:4096]))
        else:
            result = frame["result"]
            if not isinstance(result, Mapping):
                raise RuntimeMalformedReply("frontend response result must be an object")
            future.set_result(result)
        return True

    def close(self) -> None:
        """Fail in-flight requests without touching the native UI or replaying."""
        self._closed = True
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(RuntimeConnectionClosed("frontend connection was lost"))


__all__ = ["FrontendRequests"]
