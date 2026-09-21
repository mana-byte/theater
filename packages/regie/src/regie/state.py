"""Régie's orchestration projection lifecycle over the public SDK."""

from __future__ import annotations

import asyncio

from theater.frontend import FrontendClient, StateProjection, StateSynchronizer

_FOLLOW_WAIT_SECONDS = 30.0


class StateController:
    """Own one snapshot/follow projection without falling back to tree polling."""

    def __init__(self, client: FrontendClient) -> None:
        self._synchronizer = StateSynchronizer(client)
        self._lock = asyncio.Lock()
        self._follow_task: asyncio.Task[StateProjection] | None = None

    @property
    def projection(self) -> StateProjection | None:
        return self._synchronizer.projection

    async def initialize(self, *, page_size: int | None = None) -> StateProjection:
        self._interrupt_follow()
        async with self._lock:
            return await self._synchronizer.refresh(page_size=page_size)

    async def synchronize(self, *, page_size: int | None = None) -> StateProjection:
        """Advance one follow batch; SDK resnapshots after gaps and reconnects."""
        self._interrupt_follow()
        async with self._lock:
            return await self._synchronizer.synchronize_once(
                page_size=page_size,
                wait_seconds=0,
            )

    async def follow(self) -> StateProjection | None:
        """Wait on the SDK's follow lane; explicit refreshes can preempt this read."""
        async with self._lock:
            task = asyncio.create_task(
                self._synchronizer.synchronize_once(wait_seconds=_FOLLOW_WAIT_SECONDS)
            )
            self._follow_task = task
            try:
                return await task
            except asyncio.CancelledError:
                caller = asyncio.current_task()
                if caller is None or caller.cancelling():
                    raise
                return None
            finally:
                self._follow_task = None

    def _interrupt_follow(self) -> None:
        if self._follow_task is not None:
            self._follow_task.cancel()

    def acknowledge_catalogs(self, generation: int) -> bool:
        """Acknowledge only the invalidation generation a public refetch served."""
        return self._synchronizer.acknowledge_catalogs(generation)


__all__ = ["StateController"]
