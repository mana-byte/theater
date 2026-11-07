"""Writer-less frontend peer shared by the Unix-stream and HTTP transports."""

from __future__ import annotations

import asyncio
import contextlib
from abc import abstractmethod
from collections.abc import AsyncIterator, Mapping

from theater.daemon.harness_runtime.constants import FRONTEND_QUEUE_MAX
from theater.daemon.harness_runtime.frontend_requests import FrontendRequests
from theater.harness.contracts.runtime import RuntimeFrontendConnection, RuntimeNotification


class FrontendProtocolError(ValueError):
    """A frontend sent an invalid local protocol frame."""


class FrontendPeer(RuntimeFrontendConnection):
    """One authenticated frontend peer; subclasses only deliver serialized frames."""

    def __init__(self, *, include_deadline: bool = False) -> None:
        self._queue: asyncio.Queue[RuntimeNotification | None] = asyncio.Queue(FRONTEND_QUEUE_MAX)
        self._closed = False
        self._requests = FrontendRequests(self._send_frame, include_deadline=include_deadline)

    @property
    def closed(self) -> bool:
        return self._closed

    def publish(self, notification: RuntimeNotification) -> None:
        if self._closed:
            raise FrontendProtocolError("frontend connection is closed")
        if self._queue.full():
            raise FrontendProtocolError("frontend observation queue is full")
        self._queue.put_nowait(notification)

    async def request(
        self, method: str, params: Mapping[str, object], *, timeout: float
    ) -> Mapping[str, object]:
        return await self._requests.request(method, params, timeout=timeout)

    def receive_response(self, message: Mapping[str, object]) -> None:
        self._requests.receive(message)

    @abstractmethod
    async def _send_frame(self, frame: Mapping[str, object], payload: bytes) -> None:
        """Deliver one already-serialized request frame exactly once."""

    async def notifications(self) -> AsyncIterator[RuntimeNotification]:
        while True:
            if self._closed and self._queue.empty():
                return
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._requests.close()
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)


__all__ = ["FrontendPeer", "FrontendProtocolError"]
