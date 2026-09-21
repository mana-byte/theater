"""Serialized off-loop bridge I/O, drained before a cancelled caller releases ownership."""

import asyncio
import contextlib
from collections.abc import Callable


class BridgePersistence:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def run[**P, R](self, work: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
        task = asyncio.create_task(self._run(work, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Queued receipts also belong to completed effects and must reach disk.
            while not task.done():
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(task)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                task.result()
            raise

    async def _run[**P, R](self, work: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
        async with self._lock:
            return await asyncio.to_thread(work, *args, **kwargs)
