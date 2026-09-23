"""Ordered presentation work independent of Textual's input message pump."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic

from regie.latency import presentation_phase


@dataclass(slots=True)
class _Pending:
    work: Callable[[], Awaitable[object]]
    result: asyncio.Future
    action: str
    queued_at: float


class PresentationQueue:
    """Never cancel an in-flight pane move; shutdown discards only unstarted work."""

    def __init__(self) -> None:
        self._pending: deque[_Pending] = deque()
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def submit[T](self, action: str, work: Callable[[], Awaitable[T]]) -> asyncio.Future[T]:
        result: asyncio.Future[T] = asyncio.get_running_loop().create_future()
        if self._closed:
            result.cancel()
            return result
        self._pending.append(_Pending(work, result, action, monotonic()))
        if self._task is None:
            self._task = asyncio.create_task(self._drain(), name="regie-presentation")
        return result

    async def run[T](self, action: str, work: Callable[[], Awaitable[T]]) -> T:
        result = self.submit(action, work)
        try:
            return await asyncio.shield(result)
        except asyncio.CancelledError:
            result.cancel()
            raise

    async def _drain(self) -> None:
        try:
            while self._pending:
                pending = self._pending.popleft()
                if pending.result.cancelled():
                    continue
                try:
                    with presentation_phase(pending.action, pending.queued_at):
                        value = await pending.work()
                except asyncio.CancelledError:
                    pending.result.cancel()
                    raise
                except Exception as error:
                    if not pending.result.done():
                        pending.result.set_exception(error)
                else:
                    if not pending.result.done():
                        pending.result.set_result(value)
        finally:
            while self._pending:
                self._pending.popleft().result.cancel()
            self._task = None

    async def close(self) -> None:
        self._closed = True
        while self._pending:
            self._pending.popleft().result.cancel()
        if self._task is not None:
            await asyncio.shield(self._task)
