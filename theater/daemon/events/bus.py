"""Lost-wakeup-safe long-poll over diagnostic-bus appends."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Collection
from typing import NamedTuple

from theater.protocol import BUS_TAIL_MAX_WAIT_SECONDS

_BUS_SCAN_WINDOW = 2000  # ids scanned per synchronous read


class BusTail(NamedTuple):
    """Matching rows plus the highest id scanned, so filtered-out rows still advance a cursor."""

    rows: list[dict]
    next_after_id: int


class BusTailWaiter:
    """Block bus tails until the store appends a row, without polling."""

    def __init__(self, store) -> None:
        self._store = store
        self._loop: asyncio.AbstractEventLoop | None = None
        self._revision = 0
        self._waiters: set[asyncio.Event] = set()
        self._closed = False
        store.register_bus_listener(self._after_append)

    @property
    def waiter_count(self) -> int:
        return len(self._waiters)

    async def tail(
        self,
        after_id: int,
        limit: int,
        kinds: Collection[str] | None = None,
        wait_seconds: float = 0.0,
    ) -> BusTail:
        loop = asyncio.get_running_loop()
        self._loop = loop
        deadline = loop.time() + wait_seconds
        while True:
            # Capture before reading: an append after the read bumps the revision.
            observed = self._revision
            result = self._read(after_id, limit, kinds)
            remaining = deadline - loop.time()
            if result.rows or self._closed:
                return result
            if result.next_after_id > after_id:  # scanned only non-matching rows
                after_id = result.next_after_id
                await asyncio.sleep(0)  # yield between bounded scan windows
                continue
            if remaining <= 0:
                return result
            if self._revision == observed:
                await self._wait(observed, remaining)

    def _read(self, after_id: int, limit: int, kinds: Collection[str] | None) -> BusTail:
        if kinds is None:
            rows = self._store.bus_tail(limit, after_id=after_id)
            return BusTail(rows, rows[-1]["id"] if rows else after_id)
        rows, next_after_id = self._store.bus_scan(after_id, limit, kinds, window=_BUS_SCAN_WINDOW)
        return BusTail(rows, next_after_id)

    async def _wait(self, observed: int, remaining: float) -> None:
        event = asyncio.Event()
        self._waiters.add(event)
        if self._revision != observed or self._closed:
            event.set()
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(event.wait(), timeout=remaining)
        finally:
            self._waiters.discard(event)

    def _after_append(self, _row: dict) -> None:
        loop = self._loop
        if self._closed or loop is None or loop.is_closed():
            return
        if _running_loop() is loop:
            self._notify()  # Store is loop-thread-only: skip the self-pipe wakeup.
        else:
            loop.call_soon_threadsafe(self._notify)

    def _notify(self) -> None:
        self._revision += 1
        for waiter in tuple(self._waiters):
            waiter.set()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._store.unregister_bus_listener(self._after_append)
        for waiter in tuple(self._waiters):
            waiter.set()
        await asyncio.sleep(0)


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


__all__ = ["BUS_TAIL_MAX_WAIT_SECONDS", "BusTail", "BusTailWaiter"]
