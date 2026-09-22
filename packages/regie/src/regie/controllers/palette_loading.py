"""Palette-owned loading independent of cancellable search requests."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable


class PaletteLoad:
    def __init__(self, loader: Callable[[], Awaitable[None]]) -> None:
        self._loader = loader
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> None:
        if self._task is None and not self._closed:
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        await self._loader()

    async def wait(self) -> None:
        if self._task is not None:
            await asyncio.shield(self._task)

    async def close(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
