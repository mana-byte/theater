"""Régie's orchestration projection lifecycle over the public SDK."""

from __future__ import annotations

import asyncio

from theater.frontend import FrontendClient, StateProjection, StateSynchronizer


class StateController:
    """Own one snapshot/follow projection without falling back to tree polling."""

    def __init__(self, client: FrontendClient) -> None:
        self._synchronizer = StateSynchronizer(client)
        self._lock = asyncio.Lock()

    @property
    def projection(self) -> StateProjection | None:
        return self._synchronizer.projection

    async def initialize(self, *, page_size: int | None = None) -> StateProjection:
        async with self._lock:
            return await self._synchronizer.refresh(page_size=page_size)

    async def synchronize(self, *, page_size: int | None = None) -> StateProjection:
        """Advance one follow batch; SDK resnapshots after gaps and reconnects."""
        async with self._lock:
            return await self._synchronizer.synchronize_once(
                page_size=page_size,
                wait_seconds=0,
            )

    def acknowledge_catalogs(self, generation: int) -> bool:
        """Acknowledge only the invalidation generation a public refetch served."""
        return self._synchronizer.acknowledge_catalogs(generation)


__all__ = ["StateController"]
