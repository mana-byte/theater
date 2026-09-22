"""An owned state-follow loop independent of Textual's message pump."""

import asyncio
from collections.abc import Awaitable, Callable


class StateFollowLoop:
    def __init__(
        self,
        refresh: Callable[[], Awaitable[bool]],
        *,
        retry_delay: float,
        on_error: Callable[[Exception], None],
    ) -> None:
        self._refresh = refresh
        self._retry_delay = retry_delay
        self._on_error = on_error
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> None:
        if self._task is None and not self._closed:
            self._task = asyncio.create_task(self._run(), name="regie-state-follow")
            self._task.add_done_callback(self._finished)

    async def _run(self) -> None:
        while not self._closed:
            changed = await self._refresh()
            await asyncio.sleep(0 if changed else self._retry_delay)

    def _finished(self, task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as error:
            self._on_error(error)

    async def close(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
