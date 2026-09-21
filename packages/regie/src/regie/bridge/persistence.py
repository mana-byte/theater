"""Serialized off-loop bridge I/O, drained before a cancelled caller releases ownership."""

import asyncio
import contextlib
from collections.abc import Callable


class BridgePersistence:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def run[**P, R](self, work: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
        async with self._lock:
            task = asyncio.create_task(asyncio.to_thread(work, *args, **kwargs))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # A running filesystem write cannot be cancelled or outlive this fence.
                while not task.done():
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await asyncio.shield(task)
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    task.result()
                raise
