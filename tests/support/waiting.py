"""Bounded polling for async tests: `until` answers a bool, `wait_until` raises on timeout."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable


async def until(predicate: Callable[[], object], timeout: float = 5.0) -> bool:
    """Poll while the event loop runs; True once the predicate holds, False after `timeout`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return bool(predicate())


async def wait_until(
    predicate: Callable[[], object], *, timeout: float = 5.0, what: str = "the condition"
) -> None:
    """Hang detector for composed state: raise AssertionError if the predicate never holds."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.01)


async def drain() -> None:
    """Let scheduled dispatch tasks run through their first awaits."""
    for _ in range(4):
        await asyncio.sleep(0)
