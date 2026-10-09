"""An owned long-poll loop: the daemon blocks, so an idle reader makes no wakeups."""

import asyncio
from collections.abc import Awaitable, Callable

_RETRY_MIN_SECONDS = 1.0
_RETRY_MAX_SECONDS = 10.0


class LongPollLoop:
    """Call ``step(wait_seconds)`` back to back; failures back off, never hot-loop."""

    def __init__(
        self,
        step: Callable[[float], Awaitable[bool]],
        *,
        wait_seconds: float,
        name: str,
    ) -> None:
        self._step = step
        self._wait_seconds = wait_seconds
        self._name = name
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def running(self) -> bool:
        return self._task is not None

    def start(self) -> None:
        if self._task is None and not self._closed:
            self._task = asyncio.create_task(self._run(), name=self._name)

    def stop(self) -> None:
        """Cancel the in-flight read; the SDK discards the cancelled lane safely."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()

    async def _run(self) -> None:
        wait = 0.0  # The first read only primes the cursor.
        delay = _RETRY_MIN_SECONDS
        while True:
            ok = await self._step(wait)
            await asyncio.sleep(0)  # never starve the loop if a read returns without suspending
            if ok:
                wait, delay = self._wait_seconds, _RETRY_MIN_SECONDS
            else:
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RETRY_MAX_SECONDS)

    async def close(self) -> None:
        self._closed = True
        task = self._task
        self.stop()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
