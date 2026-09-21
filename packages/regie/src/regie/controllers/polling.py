"""One-at-a-time public refresh gate for Textual interval callbacks."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


class RefreshGate:
    """Drop an overlapping tick instead of issuing concurrent state.follow calls."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def run(self, callback: Callable[[], Awaitable[object]]) -> bool:
        if self._lock.locked():
            return False
        async with self._lock:
            await callback()
        return True


__all__ = ["RefreshGate"]
